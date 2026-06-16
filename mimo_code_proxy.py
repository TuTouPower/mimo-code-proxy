#!/usr/bin/env python3
"""MiMo 免费通道 -> OpenAI 兼容端点 (Docker 部署, stdlib only)。"""
import json
import os
import sys
import time
import base64
import uuid
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("MIMO_UPSTREAM", "https://api.xiaomimimo.com").rstrip("/")
BOOTSTRAP_URL = f"{UPSTREAM}/api/free-ai/bootstrap"
CHAT_URL = f"{UPSTREAM}/api/free-ai/openai/chat"
CLIENT_FILE = os.environ.get("MIMO_CLIENT_FILE", "/data/mimo-client")
LISTEN_HOST = os.environ.get("MIMO_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MIMO_PORT", "8788"))
LOCAL_KEY = os.environ.get("MIMO_KEY", "")
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

MIMO_MAX_RETRY = int(os.environ.get("MIMO_MAX_RETRY", "3"))

_active_client = None
_active_jwt = None
_active_jwt_exp = 0
_fp_lock = threading.Lock()
_health_ok = False
_health_ts = 0
_HEALTH_CACHE_S = 30


LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
LOG_LEVEL = os.environ.get("MIMO_LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in LOG_LEVELS:
    LOG_LEVEL = "INFO"
LOG_THRESHOLD = LOG_LEVELS[LOG_LEVEL]


def log(level, *a, req_id=None):
    if LOG_LEVELS.get(level, 1) < LOG_THRESHOLD:
        return
    ts = time.strftime("%H:%M:%S")
    parts = [f"[{ts}]", f"[{level}]"]
    if req_id:
        parts.append(f"[req={req_id}]")
    print(*parts, *a, file=sys.stderr, flush=True)


def new_req_id():
    return uuid.uuid4().hex[:8]


def _load_or_create_fp():
    """从 CLIENT_FILE 或 MIMO_CLIENT_ID 读取，否则生成新 UUID 并持久化。"""
    fp = os.environ.get("MIMO_CLIENT_ID", "").strip()
    if fp:
        log("DEBUG", "using MIMO_CLIENT_ID")
        return fp
    try:
        fp = open(CLIENT_FILE).read().strip()
        if fp:
            log("DEBUG", "loaded fp from file")
            return fp
    except Exception:
        pass
    fp = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(CLIENT_FILE), exist_ok=True)
        with open(CLIENT_FILE, "w") as f:
            f.write(fp)
        os.chmod(CLIENT_FILE, 0o600)
    except Exception as e:
        log("WARN", "cannot persist fingerprint", e)
    log("INFO", "generated new fp:", fp[:8] + "...")
    return fp


def _persist_fp(fp):
    try:
        os.makedirs(os.path.dirname(CLIENT_FILE), exist_ok=True)
        with open(CLIENT_FILE, "w") as f:
            f.write(fp)
        os.chmod(CLIENT_FILE, 0o600)
    except Exception as e:
        log("WARN", "cannot persist fingerprint", e)


def _replace_fingerprint(req_id=None):
    """生成新指纹，bootstrap 新 JWT，持久化。调用方需持有 _fp_lock。"""
    global _active_client, _active_jwt, _active_jwt_exp
    old = _active_client[:8] + "..." if _active_client else "none"
    _active_client = str(uuid.uuid4())
    _active_jwt = None
    _active_jwt_exp = 0
    _persist_fp(_active_client)
    log("INFO", "fingerprint rotated", old, "->", _active_client[:8] + "...", req_id=req_id)
    _active_jwt, _active_jwt_exp = _bootstrap_with(_active_client, req_id=req_id)


def _bootstrap_with(client, req_id=None):
    body = json.dumps({"client": client}).encode()
    req = urllib.request.Request(
        BOOTSTRAP_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    jwt = data.get("jwt")
    if not jwt:
        raise RuntimeError("bootstrap missing jwt")
    exp = _decode_exp(jwt)
    log("DEBUG", "bootstrap ok fp=" + client[:8] + "... exp_in=" + str(int((exp - time.time() * 1000) / 1000)) + "s", req_id=req_id)
    return jwt, exp


def ensure_jwt(req_id=None):
    """确保当前活跃指纹有有效 JWT。过期则同指纹重 bootstrap，不计入 retry。"""
    global _active_jwt, _active_jwt_exp
    with _fp_lock:
        now = time.time() * 1000
        if _active_jwt and (_active_jwt_exp - now) > REFRESH_MARGIN * 1000:
            return _active_jwt
        log("DEBUG", "JWT expired or missing, re-bootstrap same fp", req_id=req_id)
        _active_jwt, _active_jwt_exp = _bootstrap_with(_active_client, req_id=req_id)
        return _active_jwt


def _decode_exp(jwt):
    try:
        payload = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
        if isinstance(payload.get("exp"), (int, float)):
            return payload["exp"] * 1000
    except Exception:
        pass
    return time.time() * 1000 + 3600 * 1000


def upstream_chat(payload, req_id=None):
    payload = dict(payload)
    payload["model"] = UPSTREAM_MODEL
    if MIMO_GUARD_TEXT:
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
            log("DEBUG", "clamp " + f + " " + str(v) + " -> " + str(MAX_OUTPUT_TOKENS), req_id=req_id)
            payload[f] = MAX_OUTPUT_TOKENS

    log("DEBUG", "request model=" + payload.get("model", "?") +
        " stream=" + str(payload.get("stream", False)) +
        " messages=" + json.dumps(payload.get("messages", []), ensure_ascii=False),
        req_id=req_id)

    last_error = None
    for retry in range(MIMO_MAX_RETRY + 1):
        try:
            jwt = ensure_jwt(req_id=req_id)
        except Exception as e:
            log("WARN", "bootstrap failed retry=" + str(retry) + "/" + str(MIMO_MAX_RETRY), repr(e), req_id=req_id)
            if retry >= MIMO_MAX_RETRY:
                raise RuntimeError("bootstrap exhausted: " + str(e))
            try:
                with _fp_lock:
                    _replace_fingerprint(req_id=req_id)
            except Exception as e2:
                log("WARN", "fingerprint replace failed", repr(e2), req_id=req_id)
            continue

        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                CHAT_URL, data=body, method="POST",
                headers={
                    "Authorization": "Bearer " + jwt,
                    "X-Mimo-Source": "mimocode-cli-free",
                    "Content-Type": "application/json",
                },
            )
            log("DEBUG", "chat request retry=" + str(retry) + "/" + str(MIMO_MAX_RETRY),
                "fp=" + (_active_client[:8] + "..."), req_id=req_id)
            resp = urllib.request.urlopen(req, timeout=300)
            log("DEBUG", "chat response status=" + str(resp.status), req_id=req_id)
            return resp
        except urllib.error.HTTPError as e:
            last_error = e
            log("WARN", "upstream HTTP " + str(e.code) + " retry=" + str(retry) + "/" + str(MIMO_MAX_RETRY), req_id=req_id)
            if retry >= MIMO_MAX_RETRY:
                raise
            try:
                with _fp_lock:
                    _replace_fingerprint(req_id=req_id)
            except Exception as e2:
                log("WARN", "fingerprint replace failed", repr(e2), req_id=req_id)
        except Exception as e:
            last_error = e
            log("WARN", "upstream error retry=" + str(retry) + "/" + str(MIMO_MAX_RETRY), repr(e), req_id=req_id)
            if retry >= MIMO_MAX_RETRY:
                raise
            try:
                with _fp_lock:
                    _replace_fingerprint(req_id=req_id)
            except Exception as e2:
                log("WARN", "fingerprint replace failed", repr(e2), req_id=req_id)


def normalize_path(path):
    """去掉查询串和尾斜杠，剥掉可选 /v1 前缀，返回精确末段。"""
    p = path.split("?")[0].rstrip("/")
    if p.startswith("/v1/"):
        return p[3:]  # 去掉 /v1，保留 /models 或 /chat/completions
    return p


MODELS_RESP = {
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


def check_health():
    global _health_ok, _health_ts
    now = time.time()
    if _health_ok and (now - _health_ts) < _HEALTH_CACHE_S:
        return True, None
    try:
        ensure_jwt()
        _health_ok = True
        _health_ts = now
        return True, None
    except Exception as e:
        _health_ok = False
        _health_ts = now
        return False, str(e)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _auth_ok(self):
        if not LOCAL_KEY:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {LOCAL_KEY}"

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        np = normalize_path(self.path)
        if np == "/models":
            if not self._auth_ok():
                return self._json(401, {"error": {"message": "invalid key"}})
            return self._json(200, MODELS_RESP)
        if np == "/health":
            ok, err = check_health()
            if ok:
                return self._json(200, {"status": "ok", "upstream": "ok"})
            return self._json(
                503,
                {"status": "degraded", "upstream": "down", "error": err},
            )
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        req_id = new_req_id()
        log("DEBUG", "POST", self.path, req_id=req_id)
        if normalize_path(self.path) != "/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        if not self._auth_ok():
            return self._json(401, {"error": {"message": "invalid key"}})
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode())
        except Exception as e:
            return self._json(400, {"error": {"message": f"bad request: {e}"}})
        try:
            resp = upstream_chat(payload, req_id=req_id)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                obj = json.loads(body)
            except Exception:
                obj = {"error": {"message": body[:500], "code": e.code}}
            log("DEBUG", "upstream HTTPError", e.code, req_id=req_id)
            return self._json(e.code, obj)
        except Exception as e:
            log("ERROR", "upstream fatal", repr(e), req_id=req_id)
            return self._json(502, {"error": {"message": str(e)}})
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
                log("DEBUG", "non-stream response", len(body), "bytes", req_id=req_id)
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
                    log("DEBUG", "stream relay ended", repr(e), req_id=req_id)
                elapsed = int((time.time() - t0) * 1000)
                log("DEBUG", "stream done", total, "bytes", elapsed, "ms",
                    "DONE=" + str(done_seen), req_id=req_id)
        finally:
            resp.close()


def main():
    global _active_client
    _active_client = _load_or_create_fp()
    try:
        with _fp_lock:
            _active_jwt, _active_jwt_exp = _bootstrap_with(_active_client)
        log("INFO", "startup JWT ok")
    except Exception as e:
        log("ERROR", "startup bootstrap failed (will retry on request):", e)
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    auth_status = "ON" if LOCAL_KEY else "OFF"
    log("INFO", "mimo-code-proxy on http://" + LISTEN_HOST + ":" + str(LISTEN_PORT) + "  auth=" + auth_status)
    srv.serve_forever()


if __name__ == "__main__":
    main()
