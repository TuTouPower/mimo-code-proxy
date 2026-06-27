#!/usr/bin/env python3
"""核心功能冒烟测试：直连 MiMo API，验证 MimoBackend 是否可用。

默认跳过。需设 MIMO_LIVE_TEST=1 启用。
用法: MIMO_LIVE_TEST=1 MIMO_LOG_LEVEL=DEBUG python3 -m unittest tests/test_live_smoke -v
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from mimo_code_proxy import MimoBackend, _ensure_fp

FP_DIR = "/tmp/mimo_smoke_test_fp"
LIVE_ENABLED = os.environ.get("MIMO_LIVE_TEST") == "1"
live = unittest.skipUnless(LIVE_ENABLED, "set MIMO_LIVE_TEST=1 to enable live tests")


@live
class TestMimoBackendLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(FP_DIR, exist_ok=True)
        name = f"smoke-{int(time.time())}"
        cls.be = MimoBackend(
            name=name,
            proxy_url=None,
            fingerprint_dir=FP_DIR,
        )

    def test_01_bootstrap_jwt(self):
        """Bootstrap 获取 JWT。"""
        jwt = self.be.get_jwt()
        self.assertIsInstance(jwt, str)
        self.assertGreater(len(jwt), 50)
        sys.stderr.write(f"\n--- JWT 获取成功 ({len(jwt)} chars)\n")

    def test_02_chat_non_stream(self):
        """非流式聊天：发 "嗨" 应得到回复。"""
        payload = {
            "model": "mimo-auto",
            "messages": [{"role": "user", "content": "嗨"}],
            "stream": False,
        }
        resp = self.be.chat(payload)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.read().decode())
        self.assertIn("choices", body)
        content = body["choices"][0]["message"]["content"]
        self.assertIsInstance(content, str)
        self.assertGreater(len(content), 0)
        sys.stderr.write(f"\n--- 回复: {content}\n")

    def test_03_chat_stream(self):
        """流式聊天。"""
        payload = {
            "model": "mimo-auto",
            "messages": [{"role": "user", "content": "说一个字：好"}],
            "stream": True,
        }
        resp = self.be.chat(payload)
        self.assertEqual(resp.status, 200)
        chunks = []
        for line in resp:
            line = line.decode() if isinstance(line, bytes) else line
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                chunks.append(json.loads(line[6:]))
        self.assertGreater(len(chunks), 0)
        delta = chunks[0]["choices"][0].get("delta", {}).get("content", "")
        sys.stderr.write(f"\n--- 流式首块: '{delta}'\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
