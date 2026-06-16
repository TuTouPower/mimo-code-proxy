#!/usr/bin/env python3
"""Tests for mimo_code_proxy."""
import json
import os
import sys
import time
import base64
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mimo_code_proxy as proxy


class TestFingerprint(unittest.TestCase):
    def setUp(self):
        proxy._jwt = None
        proxy._jwt_exp = 0
        self._saved_client_id = os.environ.pop("MIMO_CLIENT_ID", None)

    def tearDown(self):
        if self._saved_client_id:
            os.environ["MIMO_CLIENT_ID"] = self._saved_client_id

    def test_get_fp_returns_uuid_format(self):
        fp = proxy.get_fp()
        parts = fp.split("-")
        self.assertEqual(len(parts), 5)
        self.assertEqual(len(fp), 36)

    def test_get_fp_from_env(self):
        os.environ["MIMO_CLIENT_ID"] = "my-custom-fp"
        self.assertEqual(proxy.get_fp(), "my-custom-fp")

    def test_get_fp_cached_from_file(self):
        with patch.object(proxy, "open", unittest.mock.mock_open(read_data="cached-fp")):
            fp = proxy.get_fp()
            self.assertEqual(fp, "cached-fp")

    def test_get_fp_deterministic_with_env(self):
        os.environ["MIMO_CLIENT_ID"] = "fixed-fp"
        self.assertEqual(proxy.get_fp(), "fixed-fp")
        self.assertEqual(proxy.get_fp(), "fixed-fp")


class TestJwtDecode(unittest.TestCase):
    def test_decode_valid_jwt_exp(self):
        exp = int(time.time()) + 3600
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256"}).encode()
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).rstrip(b"=").decode()
        jwt = f"{header}.{payload}.sig"
        result = proxy._decode_exp(jwt)
        self.assertEqual(result, exp * 1000)

    def test_decode_invalid_jwt_returns_fallback(self):
        now = time.time() * 1000
        result = proxy._decode_exp("garbage.jwt.token")
        self.assertAlmostEqual(result, now + 3600 * 1000, delta=5000)


class TestAuth(unittest.TestCase):
    def _make_handler(self):
        h = MagicMock(spec=proxy.Handler)
        h.headers = {}
        h._auth_ok = proxy.Handler._auth_ok.__get__(h, proxy.Handler)
        return h

    def test_auth_ok_no_key_set(self):
        proxy.LOCAL_KEY = ""
        h = self._make_handler()
        self.assertTrue(h._auth_ok())

    def test_auth_ok_correct_key(self):
        proxy.LOCAL_KEY = "sk-test-123"
        h = self._make_handler()
        h.headers = {"Authorization": "Bearer sk-test-123"}
        self.assertTrue(h._auth_ok())

    def test_auth_fail_wrong_key(self):
        proxy.LOCAL_KEY = "sk-test-123"
        h = self._make_handler()
        h.headers = {"Authorization": "Bearer wrong-key"}
        self.assertFalse(h._auth_ok())

    def test_auth_fail_missing_header(self):
        proxy.LOCAL_KEY = "sk-test-123"
        h = self._make_handler()
        h.headers = {}
        self.assertFalse(h._auth_ok())


class TestModelsResponse(unittest.TestCase):
    def test_models_response_structure(self):
        self.assertEqual(proxy.MODELS_RESP["object"], "list")
        self.assertEqual(len(proxy.MODELS_RESP["data"]), 1)
        self.assertEqual(proxy.MODELS_RESP["data"][0]["id"], "mimo-auto")


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proxy.LOCAL_KEY = "sk-test-key"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

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

    def test_health_returns_ok(self):
        resp = self._get("/v1/health")
        self.assertEqual(resp.status, 200)

    def test_health_no_auth_needed(self):
        resp = self._get("/v1/health")
        data = json.loads(resp.read())
        self.assertEqual(data["status"], "ok")

    @patch("mimo_code_proxy.get_jwt", return_value="mock-jwt")
    def test_models_with_valid_key(self, _mock):
        resp = self._get("/v1/models", {"Authorization": "Bearer sk-test-key"})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertEqual(data["object"], "list")

    def test_models_without_key_returns_401(self):
        try:
            self._get("/v1/models")
            self.fail("expected 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_models_with_wrong_key_returns_401(self):
        try:
            self._get("/v1/models", {"Authorization": "Bearer bad-key"})
            self.fail("expected 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    @patch("mimo_code_proxy.get_jwt", return_value="mock-jwt")
    @patch("mimo_code_proxy.upstream_chat")
    def test_chat_without_key_returns_401(self, _mock_chat, _mock_jwt):
        try:
            self._post("/v1/chat/completions", {"messages": []})
            self.fail("expected 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    @patch("mimo_code_proxy.get_jwt", return_value="mock-jwt")
    @patch("mimo_code_proxy.upstream_chat")
    def test_chat_success_response(self, mock_chat, _mock_jwt):
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = (
            b'{"choices":[{"message":{"content":"hello"}}]}'
        )
        mock_chat.return_value = mock_resp

        resp = self._post(
            "/v1/chat/completions",
            {"model": "mimo-auto", "messages": [{"role": "user", "content": "hi"}]},
            {"Authorization": "Bearer sk-test-key"},
        )
        self.assertEqual(resp.status, 200)

    @patch("mimo_code_proxy.get_jwt", return_value="mock-jwt")
    @patch("mimo_code_proxy.upstream_chat")
    def test_chat_upstream_502_propagates(self, mock_chat, _mock_jwt):
        mock_chat.side_effect = Exception("upstream down")

        try:
            self._post(
                "/v1/chat/completions",
                {"model": "mimo-auto", "messages": [{"role": "user", "content": "hi"}]},
                {"Authorization": "Bearer sk-test-key"},
            )
            self.fail("expected 502")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)

    def test_404_on_unknown_path(self):
        try:
            self._get("/v1/nonexistent")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_404_on_unknown_post(self):
        try:
            self._post(
                "/v1/embeddings",
                {"input": "test"},
                {"Authorization": "Bearer sk-test-key"},
            )
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    @patch("mimo_code_proxy.get_jwt", return_value="mock-jwt")
    def test_bootstrap_failure_falls_back(self, _mock):
        resp = self._get("/v1/models", {"Authorization": "Bearer sk-test-key"})
        self.assertEqual(resp.status, 200)


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

    def test_trailing_slash_models(self):
        self.assertEqual(proxy.normalize_path("/v1/models/"), "/models")

    def test_trailing_slash_chat(self):
        self.assertEqual(
            proxy.normalize_path("/chat/completions/"),
            "/chat/completions",
        )


class TestPathCompatibility(unittest.TestCase):
    """集成测试：不带 /v1 前缀的路径也能命中。"""

    @classmethod
    def setUpClass(cls):
        proxy.LOCAL_KEY = ""
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_health_no_v1_prefix(self):
        req = urllib.request.Request(self._url("/health"))
        resp = urllib.request.urlopen(req, timeout=10)
        self.assertEqual(resp.status, 200)

    def test_models_no_v1_prefix(self):
        req = urllib.request.Request(self._url("/models"))
        resp = urllib.request.urlopen(req, timeout=10)
        self.assertEqual(resp.status, 200)

    @patch("mimo_code_proxy.get_jwt", return_value="mock-jwt")
    @patch("mimo_code_proxy.upstream_chat")
    def test_chat_no_v1_prefix(self, mock_chat, _mock_jwt):
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = b'{"choices":[]}'
        mock_chat.return_value = mock_resp
        resp = urllib.request.urlopen(urllib.request.Request(
            self._url("/chat/completions"),
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ), timeout=10)
        self.assertEqual(resp.status, 200)


class TestUpstreamChatLogic(unittest.TestCase):
    def setUp(self):
        proxy._jwt = None
        proxy._jwt_exp = 0
        proxy.LOCAL_KEY = ""

    def test_guard_prompt_injected(self):
        with patch("mimo_code_proxy.get_jwt") as mock_jwt, patch(
            "urllib.request.urlopen"
        ) as mock_urlopen:
            mock_jwt.return_value = "test-jwt"
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b"{}"
            mock_urlopen.return_value = mock_resp

            proxy.upstream_chat(
                {"messages": [{"role": "user", "content": "hi"}]}
            )

            call_args = mock_urlopen.call_args[0][0]
            body = json.loads(call_args.data)
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertIn("MiMoCode", body["messages"][0]["content"])

    def test_guard_not_duplicated(self):
        with patch("mimo_code_proxy.get_jwt") as mock_jwt, patch(
            "urllib.request.urlopen"
        ) as mock_urlopen:
            mock_jwt.return_value = "test-jwt"
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b"{}"
            mock_urlopen.return_value = mock_resp

            proxy.upstream_chat(
                {
                    "messages": [
                        {"role": "system", "content": proxy.MIMO_GUARD_TEXT},
                        {"role": "user", "content": "hi"},
                    ]
                }
            )

            call_args = mock_urlopen.call_args[0][0]
            body = json.loads(call_args.data)
            self.assertEqual(len(body["messages"]), 2)

    def test_max_tokens_clamped(self):
        with patch("mimo_code_proxy.get_jwt") as mock_jwt, patch(
            "urllib.request.urlopen"
        ) as mock_urlopen:
            mock_jwt.return_value = "test-jwt"
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b"{}"
            mock_urlopen.return_value = mock_resp

            proxy.upstream_chat(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 999999,
                }
            )

            call_args = mock_urlopen.call_args[0][0]
            body = json.loads(call_args.data)
            self.assertEqual(body["max_tokens"], proxy.MAX_OUTPUT_TOKENS)

    def test_model_forced_to_mimo_auto(self):
        with patch("mimo_code_proxy.get_jwt") as mock_jwt, patch(
            "urllib.request.urlopen"
        ) as mock_urlopen:
            mock_jwt.return_value = "test-jwt"
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Type": "application/json"}
            mock_resp.read.return_value = b"{}"
            mock_urlopen.return_value = mock_resp

            proxy.upstream_chat(
                {
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )

            call_args = mock_urlopen.call_args[0][0]
            body = json.loads(call_args.data)
            self.assertEqual(body["model"], "mimo-auto")


if __name__ == "__main__":
    unittest.main()
