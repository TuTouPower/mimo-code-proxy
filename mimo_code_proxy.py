#!/usr/bin/env python3
"""MiMo 多出口代理轮询负载均衡器 -> OpenAI 兼容端点 (Docker 部署, stdlib only)。

模拟 MiMo Code CLI 的 mimo-free 扩展行为：
Bootstrap(JWT) → /api/free-ai/openai/chat
"""
import argparse
import base64
import hashlib
import json
import os
import platform
import secrets
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
MAX_OUTPUT_TOKENS = 128000
REFRESH_MARGIN = 300
JWT_REFRESH_INTERVAL = 30
USER_AGENT = "mimocode/prod/0.1.3/cli"

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


def _random_base62(n: int) -> str:
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[b % 62] for b in secrets.token_bytes(n))


def _make_session_id():
    """格式 ses_<12hex><14base62> = 26 字符，与 id/id.ts 的 Identifier.create 一致。"""
    now = int(time.time() * 1000)
    encoded = ~(now * 0x1000) & 0xFFFFFFFFFFFF
    hex_part = encoded.to_bytes(6, "big").hex()
    return "ses_" + hex_part + _random_base62(14)


_SESSION_ID = _make_session_id()

# 服务端校验 #9 + #10：system messages 中必须包含这些品牌/模型标识字符串
_MIMO_PREFIX_MESSAGES = [
    {
        "role": "system",
        "content": (
            "You are MiMoCode, an interactive CLI tool that helps users with "
            "software engineering tasks."
        ),
    },
    {
        "role": "system",
        "content": (
            "You are MiMo Code Agent, built by Xiaomi MiMo Team. "
            "You are an interactive agent that helps users with software "
            "engineering tasks."
        ),
    },
]


# ---------------------------------------------------------------------------
# 指纹：SHA256(hostname|os|arch|cpu|username)，与 mimo-free 扩展一致
# ---------------------------------------------------------------------------
_global_fp = None
_global_fp_lock = threading.Lock()
_GLOBAL_FP_FILE = "fp_global"


def _get_cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def _normalize_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("x86_64", "amd64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def _create_fp():
    hostname = platform.node()
    os_name = platform.system().lower()
    arch = _normalize_arch(platform.machine())
    cpu = _get_cpu_model()
    username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    raw = f"{hostname}|{os_name}|{arch}|{cpu}|{username}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _ensure_fp(fp_dir):
    global _global_fp
    if _global_fp:
        return _global_fp
    with _global_fp_lock:
        if _global_fp:
            return _global_fp
        fp_path = os.path.join(fp_dir, _GLOBAL_FP_FILE)
        try:
            with open(fp_path) as f:
                _global_fp = f.read().strip()
        except Exception:
            _global_fp = _create_fp()
            os.makedirs(fp_dir, exist_ok=True)
            with open(fp_path, "w") as f:
                f.write(_global_fp)
            os.chmod(fp_path, 0o600)
    return _global_fp


# ---------------------------------------------------------------------------
# MiMo 后端
# ---------------------------------------------------------------------------
class MimoBackend:
    def __init__(self, name, proxy_url, fingerprint_dir):
        self.name = name
        self.proxy_url = proxy_url
        self._fingerprint_dir = fingerprint_dir
        self.jwt = None
        self.jwt_exp = 0
        self._lock = threading.Lock()

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
            part = jwt.split(".")[1]
            pad = 4 - len(part) % 4
            if pad != 4:
                part += "=" * pad
            p = json.loads(base64.urlsafe_b64decode(part))
            if isinstance(p.get("exp"), (int, float)):
                return p["exp"] * 1000
        except Exception:
            pass
        return time.time() * 1000 + 3600 * 1000

    def _bootstrap(self):
        fp = _ensure_fp(self._fingerprint_dir)
        body = json.dumps({"client": fp}).encode()
        req = urllib.request.Request(
            BOOTSTRAP_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
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

    def start_jwt_refresher(self):
        def _run():
            while True:
                time.sleep(JWT_REFRESH_INTERVAL)
                try:
                    self.get_jwt()
                except Exception as e:
                    log("DEBUG", f"JWT refresher: {e}", backend=self.name)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def chat(self, payload, req_id=None):
        payload = dict(payload)
        payload["model"] = UPSTREAM_MODEL
        payload["temperature"] = 1.0
        payload["max_tokens"] = MAX_OUTPUT_TOKENS
        payload.pop("top_p", None)
        payload.pop("top_k", None)

        msgs = _MIMO_PREFIX_MESSAGES + list(payload.get("messages") or [])
        payload["messages"] = msgs

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
                    "User-Agent": USER_AGENT,
                    "x-session-affinity": _SESSION_ID,
                },
            )
            return opener.open(req, timeout=300)

        try:
            return _do(self.get_jwt())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log("WARN", f"got {e.code} -> refresh JWT retry",
                    req_id=req_id, backend=self.name)
                return _do(self.get_jwt(force=True))
            if e.code == 441:
                log("WARN", "upstream 441 risk control blocked",
                    req_id=req_id, backend=self.name)
            raise


# ---------------------------------------------------------------------------
# 轮询选择器
# ---------------------------------------------------------------------------
class RoundRobin:
    def __init__(self, backends):
        self._backends = backends
        self._i = 0
        self._lock = threading.Lock()

    def __len__(self):
        return len(self._backends)

    def pick(self):
        with self._lock:
            b = self._backends[self._i]
            self._i = (self._i + 1) % len(self._backends)
            return b


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
def make_handler(balancer, api_key):
    MODELS_RESP = json.dumps({
        "object": "list",
        "data": [{"id": "mimo-auto", "object": "model", "created": 0, "owned_by": "xiaomi-mimo-free"}],
    }).encode()

    _req_count = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _auth_ok(self):
            if not api_key:
                return True
            if self.headers.get("Authorization") == f"Bearer {api_key}":
                return True
            if self.headers.get("x-api-key") == api_key:
                return True
            return False

        def _respond(self, code, body_bytes, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def log_message(self, *a):
            pass

        def _log_req(self, req_id, backend, status, t_start):
            elapsed = int((time.time() - t_start) * 1000)
            log("INFO", f"status={status} elapsed={elapsed}ms",
                req_id=req_id, backend=backend)

        def do_GET(self):
            np = normalize_path(self.path)
            if np == "/models":
                if not self._auth_ok():
                    return self._respond(401, b'{"error":{"message":"invalid key"}}')
                return self._respond(200, MODELS_RESP)
            if np == "/health":
                req_id = new_req_id()
                log("DEBUG", "GET health", req_id=req_id)
                return self._respond(200, json.dumps({
                    "status": "ok", "backends": len(balancer),
                    "requests": _req_count,
                }).encode())
            self._respond(404, b'{"error":{"message":"not found"}}')

        def do_POST(self):
            req_id = new_req_id()
            t_start = time.time()
            log("DEBUG", "POST", self.path, req_id=req_id)

            np = normalize_path(self.path)
            is_anthropic = np == "/messages"
            is_openai = np == "/chat/completions"

            if not is_openai and not is_anthropic:
                self._log_req(req_id, "-", 404, t_start)
                return self._respond(404, b'{"error":{"message":"not found"}}')
            if not self._auth_ok():
                self._log_req(req_id, "-", 401, t_start)
                return self._respond(401, b'{"error":{"message":"invalid key"}}')
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n).decode())
            except Exception as e:
                self._log_req(req_id, "-", 400, t_start)
                return self._respond(400, json.dumps({"error": {"message": f"bad request: {e}"}}).encode())

            last_error = None
            tried = set()
            while len(tried) < len(balancer):
                be = balancer.pick()
                _req_count[be.name] = _req_count.get(be.name, 0) + 1
                if be.name in tried:
                    break
                tried.add(be.name)
                tag = be.name

                oai_payload = payload if is_openai else anthropic_to_openai(payload)

                try:
                    resp = be.chat(oai_payload, req_id=req_id)
                except urllib.error.HTTPError as e:
                    last_error = e
                    log("WARN", f"upstream HTTP {e.code}", req_id=req_id, backend=tag)
                    continue
                except Exception as e:
                    last_error = e
                    log("WARN", f"upstream error: {e}", req_id=req_id, backend=tag)
                    continue

                try:
                    is_stream = oai_payload.get("stream", False)
                    self.send_response(200)

                    if is_anthropic:
                        self.send_header("Content-Type", "text/event-stream" if is_stream else "application/json")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        if not is_stream:
                            oai_body = json.loads(resp.read().decode())
                            choice = (oai_body.get("choices") or [{}])[0]
                            message = choice.get("message") or {}
                            content = message.get("content") or ""
                            stop = choice.get("finish_reason", "end_turn")
                            if stop == "stop":
                                stop = "end_turn"
                            result = {
                                "id": "msg_" + uuid.uuid4().hex[:24],
                                "type": "message", "role": "assistant",
                                "content": [{"type": "text", "text": content}],
                                "model": oai_body.get("model", UPSTREAM_MODEL),
                                "stop_reason": stop,
                                "usage": {"input_tokens": 0, "output_tokens": oai_body.get("usage", {}).get("completion_tokens", 0)},
                            }
                            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
                        else:
                            model = payload.get("model", UPSTREAM_MODEL)
                            msg_id = "msg_" + uuid.uuid4().hex[:24]
                            def _emit(events):
                                for ev in events:
                                    self.wfile.write(f"event: {ev['type']}\n".encode())
                                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                                self.wfile.flush()
                            _emit([{
                                "type": "message_start",
                                "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model,
                                             "content": [], "stop_reason": None,
                                             "usage": {"input_tokens": 0, "output_tokens": 0}},
                            }])
                            total = 0
                            block_started = False
                            done_seen = False
                            try:
                                buf = b""
                                while True:
                                    chunk = resp.read(1024)
                                    if not chunk:
                                        break
                                    buf += chunk
                                    while b"\n" in buf:
                                        line, buf = buf.split(b"\n", 1)
                                        for ev in openai_sse_to_anthropic_sse_line(line):
                                            if ev["type"] == "content_block_start" and not block_started:
                                                block_started = True
                                                _emit([ev])
                                            elif ev["type"] == "content_block_delta":
                                                total += len(ev["delta"]["text"])
                                                _emit([ev])
                                            elif ev["type"] in ("content_block_stop", "message_delta", "message_stop"):
                                                done_seen = True
                                                _emit([ev])
                            except Exception:
                                pass
                            if not done_seen:
                                _emit([
                                    {"type": "content_block_stop", "index": 0},
                                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": total}},
                                    {"type": "message_stop"},
                                ])
                    else:
                        self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                        if not is_stream:
                            body = resp.read()
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                        else:
                            self.send_header("Connection", "close")
                            self.end_headers()
                            try:
                                while True:
                                    chunk = resp.read(1024)
                                    if not chunk:
                                        break
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                            except Exception:
                                pass
                finally:
                    resp.close()
                self._log_req(req_id, tag, 200, t_start)
                return

            if is_anthropic:
                self._respond(502, json.dumps({
                    "type": "error",
                    "error": {"type": "api_error", "message": f"all backends failed: {last_error}"},
                }).encode())
                self._log_req(req_id, tag, 502, t_start)
            else:
                if isinstance(last_error, urllib.error.HTTPError):
                    body = last_error.read().decode(errors="replace")
                    try:
                        obj = json.loads(body)
                    except Exception:
                        obj = {"error": {"message": body[:500], "code": last_error.code}}
                    self._log_req(req_id, tag, last_error.code, t_start)
                    return self._respond(last_error.code, json.dumps(obj).encode())
                log("ERROR", "upstream fatal", repr(last_error), req_id=req_id)
                self._log_req(req_id, tag, 502, t_start)
                return self._respond(502, json.dumps({"error": {"message": str(last_error)}}).encode())

    return Handler


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def normalize_path(path):
    p = path.split("?")[0].rstrip("/")
    if p.startswith("/v1/"):
        return p[3:]
    return p


def anthropic_to_openai(req):
    oai = {"model": req.get("model", UPSTREAM_MODEL)}
    if req.get("stream"):
        oai["stream"] = True
    if "max_tokens" in req:
        oai["max_tokens"] = req["max_tokens"]
    if "temperature" in req:
        oai["temperature"] = req["temperature"]
    if "top_p" in req:
        oai["top_p"] = req["top_p"]
    messages = []
    system = req.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        for block in system:
            if block.get("type") == "text":
                messages.append({"role": "system", "content": block.get("text", "")})
    for msg in req.get("messages", []):
        messages.append(msg)
    oai["messages"] = messages
    return oai


def openai_sse_to_anthropic_sse_line(oai_chunk_line):
    if not oai_chunk_line.startswith(b"data: "):
        return []
    payload_str = oai_chunk_line[6:].strip()
    if payload_str == "[DONE]":
        return []
    try:
        payload = json.loads(payload_str)
    except Exception:
        return []
    choice = (payload.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    finish = choice.get("finish_reason")
    events = []
    if "role" in delta and delta["role"] == "assistant":
        events.append({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    elif "content" in delta and delta["content"]:
        events.append({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta["content"]}})
    if finish:
        events.append({"type": "content_block_stop", "index": 0})
        events.append({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 0}})
        events.append({"type": "message_stop"})
    return events


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
    p = argparse.ArgumentParser(description="MiMo 多出口代理轮询负载均衡器")
    p.add_argument("-c", "--config", default=os.environ.get(
        "MIMO_CONFIG", os.path.join(os.path.dirname(__file__), "mimo_config.json")),
        help="配置文件路径")
    args = p.parse_args()

    cfg = load_config(args.config)
    backends_cfg = cfg.get("backends", [])
    api_key = cfg["api_key"]
    listen = cfg["listen"]
    fp_dir = cfg["fingerprint_dir"]

    backends = []
    for bc in backends_cfg:
        be = MimoBackend(name=bc["name"], proxy_url=bc.get("proxy"), fingerprint_dir=fp_dir)
        try:
            be.get_jwt()
            log("INFO", f"[{be.name}] 就绪 (代理: {be.proxy_url or '直连'}, 指纹: {_ensure_fp(fp_dir)[:12]}...)")
        except Exception as e:
            log("WARN", f"bootstrap 失败 (请求时重试): {e}", backend=be.name)
        be.start_jwt_refresher()
        backends.append(be)

    balancer = RoundRobin(backends)
    handler_cls = make_handler(balancer, api_key)

    host, port = listen["host"], listen["port"]
    srv = ThreadingHTTPServer((host, port), handler_cls)
    log("INFO", f"监听 http://{host}:{port}/v1  上游={CHAT_URL}  认证={'ON' if api_key else 'OFF'}  后端数={len(backends)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断信号，退出")
        srv.shutdown()


if __name__ == "__main__":
    main()
