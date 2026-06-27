#!/usr/bin/env python3
"""MiMo proxy tests (free-ai bootstrap + JWT)。"""
import io
import json
import os
import sys
import time
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import mimo_code_proxy as proxy


class TestLoadConfig(unittest.TestCase):
    def test_load_valid_config(self):
        cfg = {"listen": {"host": "127.0.0.1", "port": 8888}, "api_key": "sk-test",
               "backends": [{"name": "sg-01", "proxy": "http://127.0.0.1:7890"}, {"name": "direct", "proxy": None}]}
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(cfg))):
            result = proxy.load_config("/tmp/test.json")
        self.assertEqual(result["listen"]["port"], 8888)
        self.assertEqual(result["api_key"], "sk-test")
        self.assertEqual(len(result["backends"]), 2)

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


class TestFingerprint(unittest.TestCase):
    def setUp(self):
        proxy._global_fp = None

    def test_create_fp_format(self):
        fp = proxy._create_fp()
        self.assertEqual(len(fp), 64)
        self.assertIsInstance(fp, str)

    def test_global_singleton(self):
        proxy._global_fp = "test-fp-1234"
        result = proxy._ensure_fp("/tmp/test_fp_dir")
        self.assertEqual(result, "test-fp-1234")


class TestMimoBackend(unittest.TestCase):
    def setUp(self):
        self.fp_dir = "/tmp/test_mimo_fp"
        os.makedirs(self.fp_dir, exist_ok=True)
        for f in os.listdir(self.fp_dir):
            if f.startswith("fp_"):
                os.remove(os.path.join(self.fp_dir, f))
        proxy._global_fp = "test-fingerprint-64-chars-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def test_chat_sends_correct_headers(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.jwt = "test-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{"choices":[{"message":{"content":"hi"}}]}'

        with patch("urllib.request.OpenerDirector.open", return_value=mock_resp) as m:
            be.chat({"messages": [{"role": "user", "content": "hi"}]})
            req = m.call_args[0][0]
            self.assertEqual(req.get_header("Authorization"), "Bearer test-jwt")
            self.assertIn("mimocode-cli-free", str(req.header_items()))
            self.assertIn(proxy.USER_AGENT, str(req.header_items()))
            self.assertIn(proxy._SESSION_ID, str(req.header_items()))

    def test_chat_injects_temperature(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.jwt = "test-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{}'

        bodies = []
        with patch("urllib.request.OpenerDirector.open", return_value=mock_resp) as m:
            be.chat({"messages": [{"role": "user", "content": "hi"}]})
            bodies.append(json.loads(m.call_args[0][0].data))
        self.assertEqual(bodies[0]["temperature"], 1.0)
        self.assertEqual(bodies[0]["model"], "mimo-auto")

    def test_chat_model_forced(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.jwt = "test-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{}'
        with patch("urllib.request.OpenerDirector.open", return_value=mock_resp) as m:
            be.chat({"model": "something-else", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(json.loads(m.call_args[0][0].data)["model"], "mimo-auto")

    def test_chat_max_tokens_forced(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.jwt = "test-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{}'
        with patch("urllib.request.OpenerDirector.open", return_value=mock_resp) as m:
            be.chat({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 999999})
            self.assertEqual(json.loads(m.call_args[0][0].data)["max_tokens"], 128000)

    def test_chat_401_retries(self):
        be = proxy.MimoBackend("test", None, self.fp_dir)
        be.jwt = "old-jwt"
        be.jwt_exp = (time.time() + 3600) * 1000
        call_count = [0]

        def _fake_open(req, timeout=300):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
            mock_resp.status = 200
            return mock_resp

        with patch("urllib.request.OpenerDirector.open", side_effect=_fake_open), \
             patch.object(be, "_bootstrap") as mock_bootstrap:
            mock_bootstrap.return_value = ("new-jwt", (time.time() + 3600) * 1000)
            be.chat({"messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(call_count[0], 2)


class TestRoundRobin(unittest.TestCase):
    def test_picks_in_order(self):
        rr = proxy.RoundRobin(["a", "b", "c"])
        self.assertEqual(rr.pick(), "a")
        self.assertEqual(rr.pick(), "b")
        self.assertEqual(rr.pick(), "c")
        self.assertEqual(rr.pick(), "a")

    def test_thread_safe(self):
        rr = proxy.RoundRobin(["a", "b"])
        results = []
        lock = threading.Lock()

        def _pick():
            for _ in range(500):
                with lock:
                    results.append(rr.pick())

        threads = [threading.Thread(target=_pick) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("a"), 2500)
        self.assertEqual(results.count("b"), 2500)


class TestHandlerAuth(unittest.TestCase):
    def _inst(self, key="", hdrs=None):
        cls = proxy.make_handler(proxy.RoundRobin([proxy.MimoBackend("b", None, "/tmp/x")]), key)
        inst = cls.__new__(cls)
        m = MagicMock()
        if hdrs:
            m.get = lambda k, d=None: hdrs.get(k, d)
        else:
            m.get = lambda k, d=None: None
        inst.headers = m
        return inst

    def test_auth_ok_no_key(self):
        self.assertTrue(self._inst("")._auth_ok())

    def test_auth_fail_wrong_key(self):
        self.assertFalse(self._inst("k", {"Authorization": "Bearer x"})._auth_ok())

    def test_auth_ok_correct_key(self):
        self.assertTrue(self._inst("k", {"Authorization": "Bearer k"})._auth_ok())

    def test_auth_ok_x_api_key(self):
        self.assertTrue(self._inst("k", {"x-api-key": "k"})._auth_ok())

    def test_auth_fail_missing_header(self):
        self.assertFalse(self._inst("k")._auth_ok())


class TestPathNormalization(unittest.TestCase):
    def test_chat_with_v1(self):
        self.assertEqual(proxy.normalize_path("/v1/chat/completions"), "/chat/completions")

    def test_chat_no_v1(self):
        self.assertEqual(proxy.normalize_path("/chat/completions"), "/chat/completions")

    def test_models_with_v1(self):
        self.assertEqual(proxy.normalize_path("/v1/models"), "/models")

    def test_models_no_v1(self):
        self.assertEqual(proxy.normalize_path("/models"), "/models")

    def test_chat_with_query_string(self):
        self.assertEqual(proxy.normalize_path("/v1/chat/completions?foo=bar"), "/chat/completions")

    def test_trailing_slash(self):
        self.assertEqual(proxy.normalize_path("/v1/chat/completions/"), "/chat/completions")


class TestAnthropicMessages(unittest.TestCase):
    def test_convert_anthropic_to_openai_basic(self):
        req = {"model": "mimo-auto", "system": "You are helpful.",
               "messages": [{"role": "user", "content": "hi"}]}
        oai = proxy.anthropic_to_openai(req)
        self.assertEqual(oai["messages"][0]["role"], "system")
        self.assertEqual(oai["messages"][1]["role"], "user")

    def test_convert_anthropic_to_openai_stream(self):
        req = {"model": "mimo-auto", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        oai = proxy.anthropic_to_openai(req)
        self.assertTrue(oai["stream"])

    def test_anthropic_stream_event_format(self):
        events = proxy.openai_sse_to_anthropic_sse_line(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n')
        self.assertGreater(len(events), 0)
        self.assertEqual(events[0]["type"], "content_block_delta")


class TestServerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fp_dir = "/tmp/test_mimo_fp_server"
        os.makedirs(cls.fp_dir, exist_ok=True)
        proxy._global_fp = "test-server-fp-64-chars-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        cls.b1 = proxy.MimoBackend("be1", None, cls.fp_dir)
        cls.b1.jwt = "test-jwt-1"
        cls.b1.jwt_exp = (time.time() + 3600) * 1000
        cls.b2 = proxy.MimoBackend("be2", None, cls.fp_dir)
        cls.b2.jwt = "test-jwt-2"
        cls.b2.jwt_exp = (time.time() + 3600) * 1000
        cls.balancer = proxy.RoundRobin([cls.b1, cls.b2])
        handler_cls = proxy.make_handler(cls.balancer, "sk-test-key")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        return urllib.request.urlopen(req, timeout=10)

    def _post(self, path, body, headers=None):
        headers = dict(headers or {})
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method="POST")
        return urllib.request.urlopen(req, timeout=10)

    def test_health_returns_backend_count(self):
        resp = self._get("/health")
        data = json.loads(resp.read())
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["backends"], 2)

    def test_models_with_valid_key(self):
        resp = self._get("/v1/models", {"Authorization": "Bearer sk-test-key"})
        data = json.loads(resp.read())
        self.assertEqual(data["data"][0]["id"], "mimo-auto")

    def test_models_without_key_returns_401(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/v1/models")
        self.assertEqual(ctx.exception.code, 401)

    def test_chat_404_on_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/v1/unknown", {"test": 1}, {"Authorization": "Bearer sk-test-key"})
        self.assertEqual(ctx.exception.code, 404)

    def test_chat_success(self):
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{"choices":[{"message":{"content":"hello"}}]}'
        with patch.object(self.b1, "chat", return_value=mock_resp):
            resp = self._post("/v1/chat/completions",
                              {"messages": [{"role": "user", "content": "hi"}]},
                              {"Authorization": "Bearer sk-test-key"})
            self.assertEqual(resp.status, 200)

    def test_chat_upstream_error_propagates(self):
        with patch.object(self.b1, "chat", side_effect=Exception("down")), \
             patch.object(self.b2, "chat", side_effect=Exception("down")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/v1/chat/completions",
                           {"messages": [{"role": "user", "content": "hi"}]},
                           {"Authorization": "Bearer sk-test-key"})
            self.assertEqual(ctx.exception.code, 502)

    def test_stream_response(self):
        chunks = [b'data: {"choices":[{"delta":{"content":"h"}}]}\n\n', b'data: [DONE]\n\n']
        it = iter(chunks)
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/event-stream"}
        mock_resp.read = lambda n=1024: next(it, b"")
        mock_resp.close = MagicMock()
        with patch.object(self.b1, "chat", return_value=mock_resp):
            resp = self._post("/v1/chat/completions",
                              {"messages": [{"role": "user", "content": "hi"}], "stream": True},
                              {"Authorization": "Bearer sk-test-key"})
            self.assertEqual(resp.status, 200)

    def test_anthropic_messages_endpoint(self):
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{"choices":[{"message":{"content":"hi"},"finish_reason":"stop"}]}'
        with patch.object(self.b1, "chat", return_value=mock_resp):
            resp = self._post("/v1/messages",
                              {"messages": [{"role": "user", "content": "hi"}]},
                              {"Authorization": "Bearer sk-test-key"})
            self.assertEqual(resp.status, 200)

    def test_anthropic_404_on_unknown_post(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/v1/unknown", {"test": 1}, {"Authorization": "Bearer sk-test-key"})
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
