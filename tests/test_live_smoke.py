#!/usr/bin/env python3
"""核心功能冒烟测试：直连 MiMo API，验证 MimoBackend 是否可用。

用法: MIMO_LOG_LEVEL=DEBUG python3 test_live_smoke.py -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from mimo_code_proxy import MimoBackend

FP_DIR = "/tmp/mimo_smoke_test_fp"


class TestMimoBackendLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(FP_DIR, exist_ok=True)
        # 每次测试使用全新指纹
        import time
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

    def test_04_fingerprint_rotate(self):
        """指纹轮换后仍可正常使用。"""
        old_fp = self.be.fingerprint
        jwt = self.be._rotate_fingerprint()
        self.assertNotEqual(self.be.fingerprint, old_fp)
        self.assertIsInstance(jwt, str)
        sys.stderr.write(
            f"\n--- 指纹已轮换: {old_fp[:8]}... -> {self.be.fingerprint[:8]}...\n"
        )

        # 轮换后仍能聊天
        payload = {
            "model": "mimo-auto",
            "messages": [{"role": "user", "content": "说一个字：嗯"}],
            "stream": False,
        }
        resp = self.be.chat(payload)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.read().decode())
        self.assertIn("choices", body)
        sys.stderr.write(f"--- 轮换后回复: {body['choices'][0]['message']['content']}\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
