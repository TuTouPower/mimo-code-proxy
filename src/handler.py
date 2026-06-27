#!/usr/bin/env python3
import json
import time
import urllib.error
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import constants
from . import converter


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
            constants.log("INFO", f"status={status} elapsed={elapsed}ms",
                req_id=req_id, backend=backend)

        def do_GET(self):
            np = normalize_path(self.path)
            if np == "/models":
                if not self._auth_ok():
                    return self._respond(401, b'{"error":{"message":"invalid key"}}')
                return self._respond(200, MODELS_RESP)
            if np == "/health":
                req_id = constants.new_req_id()
                constants.log("DEBUG", "GET health", req_id=req_id)
                return self._respond(200, json.dumps({
                    "status": "ok", "backends": len(balancer),
                    "requests": _req_count,
                }).encode())
            self._respond(404, b'{"error":{"message":"not found"}}')

        def do_POST(self):
            req_id = constants.new_req_id()
            t_start = time.time()
            constants.log("DEBUG", "POST", self.path, req_id=req_id)

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

                oai_payload = payload if is_openai else converter.anthropic_to_openai(payload)

                try:
                    resp = be.chat(oai_payload, req_id=req_id)
                except urllib.error.HTTPError as e:
                    last_error = e
                    constants.log("WARN", f"upstream HTTP {e.code}", req_id=req_id, backend=tag)
                    continue
                except Exception as e:
                    last_error = e
                    constants.log("WARN", f"upstream error: {e}", req_id=req_id, backend=tag)
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
                                "model": oai_body.get("model", constants.UPSTREAM_MODEL),
                                "stop_reason": stop,
                                "usage": {"input_tokens": 0, "output_tokens": oai_body.get("usage", {}).get("completion_tokens", 0)},
                            }
                            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
                        else:
                            model = payload.get("model", constants.UPSTREAM_MODEL)
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
                                        for ev in converter.openai_sse_to_anthropic_sse_line(line):
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
                constants.log("ERROR", "upstream fatal", repr(last_error), req_id=req_id)
                self._log_req(req_id, tag, 502, t_start)
                return self._respond(502, json.dumps({"error": {"message": str(last_error)}}).encode())

    return Handler


def normalize_path(path):
    p = path.split("?")[0].rstrip("/")
    if p.startswith("/v1/"):
        return p[3:]
    return p
