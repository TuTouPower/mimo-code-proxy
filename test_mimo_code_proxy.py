#!/usr/bin/env python3
"""Tests for mimo_code_proxy with multi-backend load balancer."""
import json
import os
import sys
import time
import io
import base64
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mimo_code_proxy as proxy


class TestLoadConfig(unittest.TestCase):
    def test_load_valid_config(self):
        cfg = {
            "listen": {"host": "127.0.0.1", "port": 8888},
            "api_key": "sk-test",
            "backends": [
                {"name": "sg-01", "proxy": "http://127.0.0.1:7890"},
                {"name": "direct", "proxy": None},
            ],
            "fingerprint_dir": "/tmp/test_fp",
        }
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(cfg))):
            result = proxy.load_config("/tmp/test.json")
        self.assertEqual(result["listen"]["host"], "127.0.0.1")
        self.assertEqual(result["listen"]["port"], 8888)
        self.assertEqual(result["api_key"], "sk-test")
        self.assertEqual(len(result["backends"]), 2)
        self.assertEqual(result["backends"][0]["proxy"], "http://127.0.0.1:7890")
        self.assertIsNone(result["backends"][1]["proxy"])

    def test_load_missing_name_raises(self):
        cfg = {"backends": [{"proxy": "http://127.0.0.1:7890"}]}
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(cfg))):
            with self.assertRaises(ValueError):
                proxy.load_config("/tmp/test.json")

    def test_load_empty_backends_raises(self):
        cfg = {"backends": []}
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(cfg))):
            with self.assertRaises(ValueError):
                proxy.load_config("/tmp/test.json")

    def test_default_listen(self):
        cfg = {"backends": [{"name": "test"}]}
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(cfg))):
            result = proxy.load_config("/tmp/test.json")
        self.assertEqual(result["listen"]["host"], "0.0.0.0")
        self.assertEqual(result["listen"]["port"], 8788)


class TestMimoBackendFingerprint(unittest.TestCase):
    def setUp(self):
        self.fp_dir = "/tmp/test_mimo_fp"
        os.makedirs(self.fp_dir, exist_ok=True)
        # Clean up any existing fp files
        for f in os.listdir(self.fp_dir):
            if f.startswith("fp_"):
                os.remove(os.path.join(self.fp_dir, f))
        proxy._global_fp = None

    def tearDown(self):
        import shutil
        if os.path.exists(self.fp_dir):
            shutil.rmtree(self.fp_dir)

    def test_fp_generation(self):
        be = proxy.MimoBackend("test-be", None, self.fp_dir)
        be.ensure_fp()
        self.assertIsNotNone(be.fingerprint)
        self.assertEqual(len(be.fingerprint), 64)  # SHA256 hex

    def test_fp_persistence(self):
        be1 = proxy.MimoBackend("test-be", None, self.fp_dir)
        be1.ensure_fp()
        fp1 = be1.fingerprint

        be2 = proxy.MimoBackend("test-be", None, self.fp_dir)
        be2.ensure_fp()
        self.assertEqual(be2.fingerprint, fp1)

    def test_fp_shared_across_backends(self):
        # 全局单指纹: 不同 name 的 backend 共享同一指纹
        be1 = proxy.MimoBackend("be-1", None, self.fp_dir)
        be2 = proxy.MimoBackend("be-2", None, self.fp_dir)
        be1.ensure_fp()
        be2.ensure_fp()
        self.assertEqual(be1.fingerprint, be2.fingerprint)

    def test_fp_file_created(self):
        be = proxy.MimoBackend("test-be", None, self.fp_dir)
        be.ensure_fp()
        fp_path = os.path.join(self.fp_dir, "fp_global")
        self.assertTrue(os.path.exists(fp_path))
        with open(fp_path) as f:
            self.assertEqual(f.read().strip(), be.fingerprint)


class TestMimoBackendJwt(unittest.TestCase):
    def setUp(self):
        self.fp_dir = "/tmp/test_mimo_fp_jwt"
        os.makedirs(self.fp_dir, exist_ok=True)
        for f in os.listdir(self.fp_dir):
            if f.startswith("fp_"):
                os.remove(os.path.join(self.fp_dir, f))

    def tearDown(self):
        import shutil
        if os.path.exists(self.fp_dir):
            shutil.rmtree(self.fp_dir)

    def test_jwt_decode_exp(self):
        exp = int(time.time()) + 3600
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256"}).encode()
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).rstrip(b"=").decode()
        jwt = f"{header}.{payload}.sig"

        be = proxy.MimoBackend("test", None, self.fp_dir)
        result = be._decode_exp(jwt)
        self.assertEqual(result, exp * 1000)

    def test_jwt_refresh_on_expiry(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.fingerprint = "test-fp"
        be.jwt = "old-jwt"
        be.jwt_exp = time.time() * 1000 - 1000  # expired

        with patch.object(be, "_bootstrap") as mock_bootstrap:
            mock_bootstrap.return_value = ("new-jwt", (time.time() + 3600) * 1000)
            jwt = be.get_jwt()
            self.assertEqual(jwt, "new-jwt")
            mock_bootstrap.assert_called_once()

    def test_jwt_no_refresh_when_valid(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.fingerprint = "test-fp"
        be.jwt = "valid-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000

        with patch.object(be, "_bootstrap") as mock_bootstrap:
            jwt = be.get_jwt()
            self.assertEqual(jwt, "valid-jwt")
            mock_bootstrap.assert_not_called()

    def test_jwt_force_refresh(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.fingerprint = "test-fp"
        be.jwt = "valid-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000

        with patch.object(be, "_bootstrap") as mock_bootstrap:
            mock_bootstrap.return_value = ("forced-jwt", (time.time() + 3600) * 1000)
            jwt = be.get_jwt(force=True)
            self.assertEqual(jwt, "forced-jwt")
            mock_bootstrap.assert_called_once()


class TestMimoBackendChat(unittest.TestCase):
    def setUp(self):
        self.fp_dir = "/tmp/test_mimo_fp_chat"
        os.makedirs(self.fp_dir, exist_ok=True)
        for f in os.listdir(self.fp_dir):
            if f.startswith("fp_"):
                os.remove(os.path.join(self.fp_dir, f))

    def tearDown(self):
        import shutil
        if os.path.exists(self.fp_dir):
            shutil.rmtree(self.fp_dir)

    def test_chat_injects_guard_prompt(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.fingerprint = "test-fp"
        be.jwt = "test-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{}'
        mock_resp.status = 200

        with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
            be.chat({"messages": [{"role": "user", "content": "hi"}]})

    def test_chat_model_forced_to_mimo_auto(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.fingerprint = "test-fp"
        be.jwt = "test-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000

        captured = {}
        def _capture(req, timeout=300):
            captured["body"] = json.loads(req.data)
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b'{}'
            mock_resp.status = 200
            return mock_resp

        with patch("urllib.request.OpenerDirector.open", side_effect=_capture):
            be.chat({"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(captured["body"]["model"], "mimo-auto")

    def test_chat_401_triggers_force_refresh(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.fingerprint = "test-fp"
        be.jwt = "old-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000

        call_count = [0]
        def _fake_open(req, timeout=300):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError("url", 401, "unauthorized", {}, io.BytesIO(b"{}"))
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b'{}'
            mock_resp.status = 200
            return mock_resp

        with patch("urllib.request.OpenerDirector.open", side_effect=_fake_open), \
             patch.object(be, "_bootstrap") as mock_bootstrap:
            mock_bootstrap.return_value = ("new-jwt", (time.time() + 3600) * 1000)
            be.chat({"messages": [{"role": "user", "content": "hi"}]})
            mock_bootstrap.assert_called_once()

    def test_chat_max_tokens_clamped(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.fingerprint = "test-fp"
        be.jwt = "test-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000

        captured = {}
        def _capture(req, timeout=300):
            captured["body"] = json.loads(req.data)
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b'{}'
            mock_resp.status = 200
            return mock_resp

        with patch("urllib.request.OpenerDirector.open", side_effect=_capture):
            be.chat({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 999999})

        self.assertEqual(captured["body"]["max_tokens"], proxy.MAX_OUTPUT_TOKENS)

    def test_chat_429_rotates_fingerprint(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.fingerprint = "old-fp"
        be.jwt = "old-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000

        call_count = [0]
        def _fake_open(req, timeout=300):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError("url", 429, "rate limited", {}, io.BytesIO(b"{}"))
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
            mock_resp.status = 200
            return mock_resp

        with patch("urllib.request.OpenerDirector.open", side_effect=_fake_open), \
             patch.object(be, "_bootstrap") as mock_bootstrap:
            mock_bootstrap.return_value = ("new-jwt", (time.time() + 3600) * 1000)
            resp = be.chat({"messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(mock_bootstrap.call_count, 1)  # _rotate_fingerprint calls _bootstrap
            self.assertNotEqual(be.fingerprint, "old-fp")
            self.assertEqual(len(be.fingerprint), 64)
            # Verify _global_fp was synced to new fingerprint
            self.assertEqual(proxy._global_fp, be.fingerprint)


class TestRoundRobin(unittest.TestCase):
    def test_picks_in_order(self):
        backends = ["a", "b", "c"]
        rr = proxy.RoundRobin(backends)
        self.assertEqual(rr.pick(), "a")
        self.assertEqual(rr.pick(), "b")
        self.assertEqual(rr.pick(), "c")
        self.assertEqual(rr.pick(), "a")

    def test_thread_safe(self):
        # 10 threads x 100 picks each = 1000 picks.
        # Lock guarantees exactly 500 each, no race.
        backends = ["a", "b"]
        rr = proxy.RoundRobin(backends)
        results = []
        lock = threading.Lock()

        def _pick():
            for _ in range(100):
                r = rr.pick()
                with lock:
                    results.append(r)

        threads = [threading.Thread(target=_pick) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1000)
        count_a = results.count("a")
        count_b = results.count("b")
        self.assertEqual(count_a, 500)
        self.assertEqual(count_b, 500)


class TestHandlerAuth(unittest.TestCase):
    def _make_handler(self, api_key=""):
        balancer = proxy.RoundRobin([])
        handler_cls = proxy.make_handler(balancer, api_key)
        h = MagicMock(spec=handler_cls)
        h.headers = {}
        h._auth_ok = handler_cls._auth_ok.__get__(h, handler_cls)
        return h

    def test_auth_ok_no_key(self):
        h = self._make_handler("")
        self.assertTrue(h._auth_ok())

    def test_auth_ok_correct_key(self):
        h = self._make_handler("sk-test-123")
        h.headers = {"Authorization": "Bearer sk-test-123"}
        self.assertTrue(h._auth_ok())

    def test_auth_fail_wrong_key(self):
        h = self._make_handler("sk-test-123")
        h.headers = {"Authorization": "Bearer wrong-key"}
        self.assertFalse(h._auth_ok())

    def test_auth_fail_missing_header(self):
        h = self._make_handler("sk-test-123")
        h.headers = {}
        self.assertFalse(h._auth_ok())

    def test_auth_ok_x_api_key(self):
        h = self._make_handler("sk-test-123")
        h.headers = {"x-api-key": "sk-test-123"}
        self.assertTrue(h._auth_ok())

    def test_auth_fail_wrong_x_api_key(self):
        h = self._make_handler("sk-test-123")
        h.headers = {"x-api-key": "wrong-key"}
        self.assertFalse(h._auth_ok())


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fp_dir = "/tmp/test_mimo_fp_server"
        os.makedirs(cls.fp_dir, exist_ok=True)
        for f in os.listdir(cls.fp_dir):
            if f.startswith("fp_"):
                os.remove(os.path.join(cls.fp_dir, f))

        # Create mock backends
        be1 = proxy.MimoBackend("be1", None, cls.fp_dir)
        be1.fingerprint = "test-fp-1"
        be1.jwt = "test-jwt-1"
        be1.jwt_exp = (time.time() + 3600) * 1000

        be2 = proxy.MimoBackend("be2", None, cls.fp_dir)
        be2.fingerprint = "test-fp-2"
        be2.jwt = "test-jwt-2"
        be2.jwt_exp = (time.time() + 3600) * 1000

        cls.backends = [be1, be2]
        cls.balancer = proxy.RoundRobin(cls.backends)
        handler_cls = proxy.make_handler(cls.balancer, "sk-test-key")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        import shutil
        if os.path.exists(cls.fp_dir):
            shutil.rmtree(cls.fp_dir)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path, headers=None):
        req = urllib.request.Request(self._url(path), headers=headers or {})
        return urllib.request.urlopen(req, timeout=10)

    def _post(self, path, body, headers=None):
        headers = dict(headers or {})
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self._url(path), data=data, headers=headers, method="POST"
        )
        return urllib.request.urlopen(req, timeout=10)

    def test_health_returns_backend_count(self):
        resp = self._get("/health")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["backends"], 2)

    def test_models_with_valid_key(self):
        resp = self._get("/v1/models", {"Authorization": "Bearer sk-test-key"})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertEqual(data["object"], "list")
        self.assertEqual(data["data"][0]["id"], "mimo-auto")

    def test_models_without_key_returns_401(self):
        try:
            self._get("/v1/models")
            self.fail("expected 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_chat_success(self):
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = (
            b'{"choices":[{"message":{"content":"hello"}}]}'
        )

        with patch.object(self.backends[0], "chat", return_value=mock_resp):
            resp = self._post(
                "/v1/chat/completions",
                {"model": "mimo-auto", "messages": [{"role": "user", "content": "hi"}]},
                {"Authorization": "Bearer sk-test-key"},
            )
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read())
            self.assertEqual(body["choices"][0]["message"]["content"], "hello")

    def test_chat_404_on_unknown_path(self):
        try:
            self._post("/v1/embeddings", {"input": "test"}, {"Authorization": "Bearer sk-test-key"})
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_chat_upstream_error_propagates(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for be in self.backends:
                stack.enter_context(patch.object(be, "chat", side_effect=Exception("upstream down")))
            try:
                self._post(
                    "/v1/chat/completions",
                    {"model": "mimo-auto", "messages": [{"role": "user", "content": "hi"}]},
                    {"Authorization": "Bearer sk-test-key"},
                )
                self.fail("expected 502")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 502)

    def test_stream_response(self):
        chunks = [
            b'data: {"choices":[{"delta":{"content":"h"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"i"}}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        reads = iter(chunks)

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/event-stream"}

        def _read(n=1024):
            try:
                return next(reads)
            except StopIteration:
                return b""

        mock_resp.read = _read

        with patch.object(self.backends[0], "chat", return_value=mock_resp):
            resp = self._post(
                "/chat/completions",
                {"model": "mimo-auto", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                {"Authorization": "Bearer sk-test-key"},
            )
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "text/event-stream")
            body = resp.read()
            self.assertIn(b"data:", body)
            self.assertIn(b"[DONE]", body)


class TestAnthropicMessages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fp_dir = "/tmp/test_mimo_fp_anthropic"
        os.makedirs(cls.fp_dir, exist_ok=True)
        for f in os.listdir(cls.fp_dir):
            if f.startswith("fp_"):
                os.remove(os.path.join(cls.fp_dir, f))

        be = proxy.MimoBackend("be1", None, cls.fp_dir)
        be.fingerprint = "test-fp-anth"
        be.jwt = "test-jwt-anth"
        be.jwt_exp = (time.time() + 3600) * 1000

        cls.backends = [be]
        cls.balancer = proxy.RoundRobin(cls.backends)
        handler_cls = proxy.make_handler(cls.balancer, "sk-test-key")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        import shutil
        if os.path.exists(cls.fp_dir):
            shutil.rmtree(cls.fp_dir)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path, body, headers=None):
        headers = dict(headers or {})
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self._url(path), data=data, headers=headers, method="POST"
        )
        return urllib.request.urlopen(req, timeout=10)

    def test_convert_anthropic_to_openai_basic(self):
        req = {
            "model": "mimo-auto",
            "max_tokens": 100,
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hi"}],
        }
        oai = proxy.anthropic_to_openai(req)
        self.assertEqual(oai["model"], "mimo-auto")
        self.assertEqual(oai["max_tokens"], 100)
        self.assertEqual(oai["messages"][0]["role"], "system")
        self.assertEqual(oai["messages"][0]["content"], "You are helpful.")
        self.assertEqual(oai["messages"][1]["role"], "user")
        self.assertEqual(oai["messages"][1]["content"], "hi")

    def test_convert_anthropic_multimodal_content(self):
        req = {
            "model": "mimo-auto",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image", "source": {"type": "base64", "data": "abc"}},
                    ],
                }
            ],
        }
        oai = proxy.anthropic_to_openai(req)
        content = oai["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "What is this?")

    def test_convert_anthropic_to_openai_stream(self):
        req = {
            "model": "mimo-auto",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
        oai = proxy.anthropic_to_openai(req)
        self.assertTrue(oai.get("stream"))

    def test_anthropic_response_format(self):
        resp = {
            "id": "msg_xxx",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "model": "mimo-auto",
            "stop_reason": "end_turn",
        }
        body = json.dumps(resp).encode()
        result = json.loads(body)
        self.assertEqual(result["type"], "message")
        self.assertEqual(result["role"], "assistant")
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["stop_reason"], "end_turn")

    def test_anthropic_stream_event_format(self):
        events = [
            {"type": "message_start", "message": {"id": "msg_xxx", "type": "message", "role": "assistant", "model": "mimo-auto", "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        result = proxy.build_anthropic_sse(events)
        self.assertIn(b"event: message_start", result)
        self.assertIn(b"event: content_block_delta", result)
        self.assertIn(b"event: message_stop", result)

    def test_server_anthropic_messages_endpoint(self):
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "hello from upstream"}}],
        }).encode()

        with patch.object(self.backends[0], "chat", return_value=mock_resp):
            resp = self._post(
                "/v1/messages",
                {
                    "model": "mimo-auto",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                {"Authorization": "Bearer sk-test-key"},
            )
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
            self.assertEqual(data["type"], "message")
            self.assertEqual(data["role"], "assistant")
            self.assertEqual(data["stop_reason"], "end_turn")
            self.assertIn("content", data)

    def test_server_anthropic_stream_endpoint(self):
        oai_chunks = [
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        reads = iter(oai_chunks)

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/event-stream"}
        def _read(n=1024):
            try:
                return next(reads)
            except StopIteration:
                return b""
        mock_resp.read = _read

        with patch.object(self.backends[0], "chat", return_value=mock_resp):
            resp = self._post(
                "/v1/messages",
                {
                    "model": "mimo-auto",
                    "max_tokens": 100,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                {"Authorization": "Bearer sk-test-key"},
            )
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "text/event-stream")
            body = resp.read()
            self.assertIn(b"event: message_start", body)
            self.assertIn(b"event: content_block_delta", body)
            self.assertIn(b"event: message_stop", body)

    def test_server_anthropic_with_system(self):
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}],
        }).encode()

        with patch.object(self.backends[0], "chat", return_value=mock_resp):
            resp = self._post(
                "/v1/messages",
                {
                    "model": "mimo-auto",
                    "max_tokens": 100,
                    "system": "You are a bot.",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                {"Authorization": "Bearer sk-test-key"},
            )
            self.assertEqual(resp.status, 200)

    def test_server_anthropic_404_on_unknown_post(self):
        try:
            self._post(
                "/v1/something_else",
                {"model": "mimo-auto", "messages": []},
                {"Authorization": "Bearer sk-test-key"},
            )
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


class TestPathNormalization(unittest.TestCase):
    def test_models_no_v1(self):
        self.assertEqual(proxy.normalize_path("/models"), "/models")

    def test_models_with_v1(self):
        self.assertEqual(proxy.normalize_path("/v1/models"), "/models")

    def test_chat_no_v1(self):
        self.assertEqual(proxy.normalize_path("/chat/completions"), "/chat/completions")

    def test_chat_with_v1(self):
        self.assertEqual(proxy.normalize_path("/v1/chat/completions"), "/chat/completions")

    def test_chat_with_query_string(self):
        self.assertEqual(
            proxy.normalize_path("/v1/chat/completions?foo=bar"),
            "/chat/completions",
        )

    def test_trailing_slash(self):
        self.assertEqual(proxy.normalize_path("/v1/models/"), "/models")
        self.assertEqual(proxy.normalize_path("/chat/completions/"), "/chat/completions")


if __name__ == "__main__":
    unittest.main()
