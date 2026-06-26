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
            part = jwt.split(".")[1]
            pad = 4 - len(part) % 4
            p = json.loads(base64.urlsafe_b64decode(part + "=" * pad))
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

    def _rotate_fingerprint(self, req_id=None):
        """生成新指纹，重新 bootstrap JWT。"""
        old_fp = self.fingerprint[:8] + "..." if self.fingerprint else "none"
        self.fingerprint = self._create_fp()
        self.jwt = None
        self.jwt_exp = 0
        log("INFO", f"fingerprint rotated {old_fp} -> {self.fingerprint[:8]}...", req_id=req_id, backend=self.name)
        self.jwt, self.jwt_exp = self._bootstrap()
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
            if e.code == 429:
                log(
                    "WARN",
                    f"rate limited -> rotate fingerprint",
                    req_id=req_id,
                    backend=self.name,
                )
                with self._lock:
                    self._rotate_fingerprint(req_id=req_id)
                return _do(self.get_jwt())
            if e.code == 441:
                log(
                    "WARN",
                    "upstream 441 risk control blocked",
                    req_id=req_id,
                    backend=self.name,
                )
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

    ANTHROPIC_MSG_RESP_ID = "msg_" + uuid.uuid4().hex[:24]
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
                return self._respond(
                    200,
                    json.dumps({"status": "ok", "backends": len(balancer), "requests": _req_count}).encode(),
                )
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
                return self._respond(
                    400,
                    json.dumps({"error": {"message": f"bad request: {e}"}}).encode(),
                )

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

                # Success - relay response
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
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "text", "text": content}],
                                "model": oai_body.get("model", UPSTREAM_MODEL),
                                "stop_reason": stop,
                                "usage": {"input_tokens": 0, "output_tokens": oai_body.get("usage", {}).get("completion_tokens", 0)},
                            }
                            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
                            log("DEBUG", "anthropic non-stream response", len(content), "chars", req_id=req_id, backend=tag)
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
                                "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}},
                            }])
                            total = 0
                            t0 = time.time()
                            done_seen = False
                            block_started = False
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
                            except Exception as e:
                                log("DEBUG", "anthropic stream relay ended", repr(e), req_id=req_id, backend=tag)
                            if not done_seen:
                                _emit([
                                    {"type": "content_block_stop", "index": 0},
                                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": total}},
                                    {"type": "message_stop"},
                                ])
                            elapsed = int((time.time() - t0) * 1000)
                            log("DEBUG", "anthropic stream done", total, "chars", elapsed, "ms", f"DONE={done_seen}", req_id=req_id, backend=tag)
                    else:
                        self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                        if not is_stream:
                            body = resp.read()
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            log("DEBUG", "non-stream response", len(body), "bytes", req_id=req_id, backend=tag)
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
                                log("DEBUG", "stream relay ended", repr(e), req_id=req_id, backend=tag)
                            elapsed = int((time.time() - t0) * 1000)
                            log("DEBUG", "stream done", total, "bytes", elapsed, "ms", f"DONE={done_seen}", req_id=req_id, backend=tag)
                finally:
                    resp.close()
                self._log_req(req_id, tag, 200, t_start)
                return

            # All backends failed
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
                    log("DEBUG", "upstream HTTPError", last_error.code, req_id=req_id)
                    self._log_req(req_id, tag, last_error.code, t_start)
                    return self._respond(last_error.code, json.dumps(obj).encode())
                log("ERROR", "upstream fatal", repr(last_error), req_id=req_id)
                self._log_req(req_id, tag, 502, t_start)
                return self._respond(
                    502, json.dumps({"error": {"message": str(last_error)}}).encode(),
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


# ---------------------------------------------------------------------------
# Anthropic Messages API 格式转换
# ---------------------------------------------------------------------------
def anthropic_to_openai(req):
    """Anthropic /v1/messages 请求体 → OpenAI /v1/chat/completions 请求体。"""
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


def build_anthropic_sse(events):
    """将 Anthropic SSE 事件列表拼成 bytes 输出。"""
    out = []
    for ev in events:
        out.append(f"event: {ev['type']}\n".encode())
        out.append(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
    return b"".join(out)


def openai_sse_to_anthropic_sse_line(oai_chunk_line):
    """解析一行 OpenAI SSE chunk → Anthropic SSE 事件列表。

    Input: b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    Output: list of dict events (empty if nothing to emit)
    """
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
        events.append({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
    elif "content" in delta and delta["content"]:
        events.append({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": delta["content"]},
        })

    if finish:
        events.append({"type": "content_block_stop", "index": 0})
        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 0},
        })
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
            log(
                "WARN",
                f"bootstrap 失败 (请求时重试): {e}",
                backend=be.name,
            )
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
