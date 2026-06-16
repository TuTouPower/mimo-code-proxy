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

_jwt = None
_jwt_exp = 0
_lock = threading.Lock()


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


def get_fp():
    fp = os.environ.get("MIMO_CLIENT_ID", "").strip()
    if fp:
        log("INFO", "using MIMO_CLIENT_ID")
        return fp
    try:
        fp = open(CLIENT_FILE).read().strip()
        if fp:
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
        log("WARN", "warn: cannot persist fingerprint", e)
    log("INFO", "generated new fp:", fp)
    return fp


def _decode_exp(jwt):
    try:
        payload = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
        if isinstance(payload.get("exp"), (int, float)):
            return payload["exp"] * 1000
    except Exception:
        pass
    return time.time() * 1000 + 3600 * 1000


def _bootstrap():
    body = json.dumps({"client": get_fp()}).encode()
    req = urllib.request.Request(
        BOOTSTRAP_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    jwt = data.get("jwt")
    if not jwt:
        raise RuntimeError("bootstrap missing jwt")
    return jwt, _decode_exp(jwt)


def get_jwt(force=False):
    global _jwt, _jwt_exp
    with _lock:
        now = time.time() * 1000
        if not force and _jwt and (_jwt_exp - now) > REFRESH_MARGIN * 1000:
            return _jwt
        _jwt, _jwt_exp = _bootstrap()
        log("DEBUG", "JWT refreshed, exp in " + str(int((_jwt_exp - now) / 1000)) + "s")
        return _jwt


def normalize_path(path):
    """去掉查询串和尾斜杠，剥掉可选 /v1 前缀，返回精确末段。"""
    p = path.split("?")[0].rstrip("/")
    if p.startswith("/v1/"):
        return p[3:]  # 去掉 /v1，保留 /models 或 /chat/completions
    return p


def upstream_chat(payload):
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
            log("DEBUG", "clamp " + f + " " + str(v) + " -> " + str(MAX_OUTPUT_TOKENS))
            payload[f] = MAX_OUTPUT_TOKENS

    def _do(jwt):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            CHAT_URL, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {jwt}",
                "X-Mimo-Source": "mimocode-cli-free",
                "Content-Type": "application/json",
            },
        )
        return urllib.request.urlopen(req, timeout=300)

    try:
        return _do(get_jwt())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log("INFO", "got " + str(e.code) + " -> refresh JWT retry")
            return _do(get_jwt(force=True))
        raise


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
            return self._json(200, {"status": "ok"})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
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
            resp = upstream_chat(payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                obj = json.loads(body)
            except Exception:
                obj = {"error": {"message": body[:500], "code": e.code}}
            return self._json(e.code, obj)
        except Exception as e:
            return self._json(502, {"error": {"message": str(e)}})
        self.send_response(200)
        self.send_header(
            "Content-Type",
            resp.headers.get("Content-Type", "application/json"),
        )
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:
            log("DEBUG", "stream relay ended", repr(e))
        finally:
            resp.close()


def main():
    get_fp()
    try:
        get_jwt()
        log("INFO", "startup JWT ok")
    except Exception as e:
        log("ERROR", "startup bootstrap failed (will retry on request):", e)
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    auth_status = "ON" if LOCAL_KEY else "OFF"
    log("INFO", "mimo-code-proxy on http://" + LISTEN_HOST + ":" + str(LISTEN_PORT) + "  auth=" + auth_status)
    srv.serve_forever()


if __name__ == "__main__":
    main()
