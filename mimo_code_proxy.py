#!/usr/bin/env python3
"""MiMo 多出口代理轮询负载均衡器 -> OpenAI 兼容端点 (Docker 部署, stdlib only)。"""
import argparse
import base64
import hashlib
import json
import os
import platform
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = os.environ.get(
    "MIMO_FREE_BASE_URL", "https://api.xiaomimimo.com"
).rstrip("/")
BOOTSTRAP_URL = f"{UPSTREAM_BASE}/api/free-ai/bootstrap"
CHAT_URL = f"{UPSTREAM_BASE}/api/free-ai/openai/chat"
UPSTREAM_MODEL = "mimo-auto"
MAX_OUTPUT_TOKENS = 131072
REFRESH_MARGIN = 300

MIMO_GUARD_TEXT = (
    "You are MiMoCode, an interactive CLI tool that helps users with "
    "software engineering tasks. Use the instructions below and the tools "
    "available to you to assist the user.\n\n"
    "IMPORTANT: You must NEVER generate or guess URLs for the user unless you "
    "are confident that the URLs are for helping the user with programming. "
    "You may use URLs provided by the user in their messages or local files.\n\n"
    "IMPORTANT: Assist with authorized security testing, defensive security, "
    "CTF challenges, and educational contexts."
)

LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
LOG_LEVEL = os.environ.get("MIMO_LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in LOG_LEVELS:
    LOG_LEVEL = "INFO"
LOG_THRESHOLD = LOG_LEVELS[LOG_LEVEL]


def log(level, *a, req_id=None, backend=None):
    if LOG_LEVELS.get(level, 1) < LOG_THRESHOLD:
        return
    ts = time.strftime("%H:%M:%S")
    parts = [f"[{ts}]", f"[{level}]"]
    if req_id:
        parts.append(f"[req={req_id}]")
    if backend:
        parts.append(f"[{backend}]")
    print(*parts, *a, file=sys.stderr, flush=True)


def new_req_id():
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# MiMo 后端: 独立指纹 + JWT + 出口代理
# ---------------------------------------------------------------------------
class MimoBackend:
    def __init__(self, name, proxy_url, fingerprint_dir):
        self.name = name
        self.proxy_url = proxy_url
        self._fingerprint_dir = fingerprint_dir
        self.fingerprint = None
        self.jwt = None
        self.jwt_exp = 0
        self._lock = threading.Lock()

    def _fp_path(self):
        return os.path.join(self._fingerprint_dir, f"fp_{self.name}")

    def _load_fp(self):
        try:
            with open(self._fp_path()) as f:
                return f.read().strip()
        except Exception:
            return None

    def _save_fp(self, fp):
        os.makedirs(self._fingerprint_dir, exist_ok=True)
        with open(self._fp_path(), "w") as f:
            f.write(fp)
        os.chmod(self._fp_path(), 0o600)

    def _create_fp(self):
        raw = "|".join(
            [
                self.name,
                platform.node(),
                "linux",
                "x64",
                platform.processor() or "x86_64",
                str(random.random()),
            ]
        )
        fp = hashlib.sha256(raw.encode()).hexdigest()
        self._save_fp(fp)
        return fp

    def ensure_fp(self):
        if self.fingerprint:
            return
        self.fingerprint = self._load_fp() or self._create_fp()

    def _make_opener(self):
        if not self.proxy_url:
            return urllib.request.build_opener()
        return urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": self.proxy_url, "https": self.proxy_url}
            )
        )

    def _decode_exp(self, jwt):
        try:
            p = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
            if isinstance(p.get("exp"), (int, float)):
                return p["exp"] * 1000
        except Exception:
            pass
        return time.time() * 1000 + 3600 * 1000

    def _bootstrap(self):
        self.ensure_fp()
        body = json.dumps({"client": self.fingerprint}).encode()
        req = urllib.request.Request(
            BOOTSTRAP_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self._make_opener().open(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        jwt = data.get("jwt")
        if not jwt:
            raise RuntimeError(f"[{self.name}] bootstrap missing jwt")
        return jwt, self._decode_exp(jwt)

    def get_jwt(self, force=False):
        with self._lock:
            now = time.time() * 1000
            if (
                not force
                and self.jwt
                and (self.jwt_exp - now) > REFRESH_MARGIN * 1000
            ):
                return self.jwt
            self.jwt, self.jwt_exp = self._bootstrap()
            log(
                "INFO",
                f"JWT refreshed, exp in {int((self.jwt_exp - now) / 1000)}s",
                backend=self.name,
            )
            return self.jwt

    def chat(self, payload, req_id=None):
        payload = dict(payload)
        payload["model"] = UPSTREAM_MODEL

        msgs = list(payload.get("messages") or [])
        already = (
            msgs
            and msgs[0].get("role") == "system"
            and isinstance(msgs[0].get("content"), str)
            and msgs[0]["content"].startswith(MIMO_GUARD_TEXT[:80])
        )
        if not already:
            msgs.insert(0, {"role": "system", "content": MIMO_GUARD_TEXT})
            payload["messages"] = msgs

        for f in ("max_tokens", "max_completion_tokens"):
            v = payload.get(f)
            if isinstance(v, int) and v > MAX_OUTPUT_TOKENS:
                log(
                    "DEBUG",
                    f"clamp {f} {v} -> {MAX_OUTPUT_TOKENS}",
                    req_id=req_id,
                    backend=self.name,
                )
                payload[f] = MAX_OUTPUT_TOKENS

        opener = self._make_opener()

        def _do(jwt):
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                CHAT_URL,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "X-Mimo-Source": "mimocode-cli-free",
                    "Content-Type": "application/json",
                },
            )
            return opener.open(req, timeout=300)

        try:
            return _do(self.get_jwt())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log(
                    "WARN",
                    f"got {e.code} -> refresh JWT retry",
                    req_id=req_id,
                    backend=self.name,
                )
                return _do(self.get_jwt(force=True))
            raise


# ---------------------------------------------------------------------------
# 轮询选择器
# ---------------------------------------------------------------------------
class RoundRobin:
    def __init__(self, backends):
        self._backends = backends
        self._i = 0
        self._lock = threading.Lock()

    def pick(self):
        with self._lock:
            b = self._backends[self._i]
            self._i = (self._i + 1) % len(self._backends)
            return b


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
def make_handler(balancer, api_key):
    MODELS_RESP = json.dumps(
        {
            "object": "list",
            "data": [
                {
                    "id": "mimo-auto",
                    "object": "model",
                    "created": 0,
                    "owned_by": "xiaomi-mimo-free",
                }
            ],
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _auth_ok(self):
            if not api_key:
                return True
            return self.headers.get("Authorization") == f"Bearer {api_key}"

        def _respond(self, code, body_bytes, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def log_message(self, *a):
            pass

        def do_GET(self):
            np = normalize_path(self.path)
            if np == "/models":
                if not self._auth_ok():
                    return self._respond(
                        401, b'{"error":{"message":"invalid key"}}'
                    )
                return self._respond(200, MODELS_RESP)
            if np == "/health":
                req_id = new_req_id()
                log("DEBUG", "GET health", req_id=req_id)
                return self._respond(
                    200,
                    json.dumps(
                        {
                            "status": "ok",
                            "backends": len(balancer._backends),
                        }
                    ).encode(),
                )
            self._respond(404, b'{"error":{"message":"not found"}}')

        def do_POST(self):
            req_id = new_req_id()
            log("DEBUG", "POST", self.path, req_id=req_id)
            if normalize_path(self.path) != "/chat/completions":
                return self._respond(404, b'{"error":{"message":"not found"}}')
            if not self._auth_ok():
                return self._respond(
                    401, b'{"error":{"message":"invalid key"}}'
                )
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n).decode())
            except Exception as e:
                return self._respond(
                    400,
                    json.dumps({"error": {"message": f"bad request: {e}"}}).encode(),
                )

            last_error = None
            tried = set()
            while len(tried) < len(balancer._backends):
                be = balancer.pick()
                if be.name in tried:
                    break
                tried.add(be.name)
                tag = be.name
                try:
                    resp = be.chat(payload, req_id=req_id)
                except urllib.error.HTTPError as e:
                    last_error = e
                    log(
                        "WARN",
                        f"upstream HTTP {e.code}",
                        req_id=req_id,
                        backend=tag,
                    )
                    continue
                except Exception as e:
                    last_error = e
                    log(
                        "WARN",
                        f"upstream error: {e}",
                        req_id=req_id,
                        backend=tag,
                    )
                    continue

                # Success - relay response
                try:
                    is_stream = payload.get("stream", False)
                    self.send_response(200)
                    content_type = resp.headers.get("Content-Type", "application/json")
                    self.send_header("Content-Type", content_type)
                    if not is_stream:
                        body = resp.read()
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(body)
                        log(
                            "DEBUG",
                            "non-stream response",
                            len(body),
                            "bytes",
                            req_id=req_id,
                            backend=tag,
                        )
                    else:
                        self.send_header("Connection", "close")
                        self.end_headers()
                        total = 0
                        t0 = time.time()
                        done_seen = False
                        try:
                            while True:
                                chunk = resp.read(1024)
                                if not chunk:
                                    break
                                total += len(chunk)
                                self.wfile.write(chunk)
                                self.wfile.flush()
                                if b"[DONE]" in chunk:
                                    done_seen = True
                        except Exception as e:
                            log(
                                "DEBUG",
                                "stream relay ended",
                                repr(e),
                                req_id=req_id,
                                backend=tag,
                            )
                        elapsed = int((time.time() - t0) * 1000)
                        log(
                            "DEBUG",
                            "stream done",
                            total,
                            "bytes",
                            elapsed,
                            "ms",
                            f"DONE={done_seen}",
                            req_id=req_id,
                            backend=tag,
                        )
                finally:
                    resp.close()
                return

            # All backends failed
            if isinstance(last_error, urllib.error.HTTPError):
                body = last_error.read().decode(errors="replace")
                try:
                    obj = json.loads(body)
                except Exception:
                    obj = {"error": {"message": body[:500], "code": last_error.code}}
                log("DEBUG", "upstream HTTPError", last_error.code, req_id=req_id)
                return self._respond(last_error.code, json.dumps(obj).encode())

            log("ERROR", "upstream fatal", repr(last_error), req_id=req_id)
            return self._respond(
                502, json.dumps({"error": {"message": str(last_error)}}).encode()
            )

    return Handler


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def normalize_path(path):
    """去掉查询串和尾斜杠，剥掉可选 /v1 前缀，返回精确末段。"""
    p = path.split("?")[0].rstrip("/")
    if p.startswith("/v1/"):
        return p[3:]
    return p


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)

    cfg.setdefault("listen", {})
    cfg["listen"].setdefault("host", "0.0.0.0")
    cfg["listen"].setdefault("port", 8788)
    cfg.setdefault("api_key", "")
    cfg.setdefault(
        "fingerprint_dir",
        os.path.join(os.path.dirname(__file__), "mimo_fingerprints"),
    )
    backends = cfg.get("backends", [])
    if not backends:
        raise ValueError("配置文件中没有 backend")
    for b in backends:
        if "name" not in b:
            raise ValueError("每个 backend 必须包含 'name'")
        b.setdefault("proxy", None)
    return cfg


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="MiMo 多出口代理轮询负载均衡器"
    )
    p.add_argument(
        "-c",
        "--config",
        default=os.environ.get(
            "MIMO_CONFIG",
            os.path.join(os.path.dirname(__file__), "mimo_config.json"),
        ),
        help="配置文件路径 (默认: ./mimo_config.json 或 $MIMO_CONFIG)",
    )
    args = p.parse_args()

    cfg = load_config(args.config)

    backends_cfg = cfg.get("backends", [])
    api_key = cfg["api_key"]
    listen = cfg["listen"]
    fp_dir = cfg["fingerprint_dir"]

    backends = []
    for bc in backends_cfg:
        be = MimoBackend(
            name=bc["name"],
            proxy_url=bc.get("proxy"),
            fingerprint_dir=fp_dir,
        )
        try:
            be.get_jwt()
            log(
                "INFO",
                f"[{be.name}] 就绪 (代理: {be.proxy_url or '直连'}, "
                f"指纹: {be.fingerprint[:12]}...)",
            )
        except Exception as e:
            log(f"[{be.name}] bootstrap 失败 (请求时重试): {e}")
        backends.append(be)

    balancer = RoundRobin(backends)
    handler_cls = make_handler(balancer, api_key)

    host, port = listen["host"], listen["port"]
    srv = ThreadingHTTPServer((host, port), handler_cls)
    log(
        "INFO",
        f"监听 http://{host}:{port}/v1  "
        f"认证={'ON' if api_key else 'OFF'}  "
        f"后端数={len(backends)}",
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断信号，退出")
        srv.shutdown()


if __name__ == "__main__":
    main()
