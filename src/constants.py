#!/usr/bin/env python3
import json
import os
import secrets
import sys
import time
import uuid



UPSTREAM_BASE = os.environ.get(
    "MIMO_FREE_BASE_URL", "https://api.xiaomimimo.com"
).rstrip("/")
BOOTSTRAP_URL = f"{UPSTREAM_BASE}/api/free-ai/bootstrap"
CHAT_URL = f"{UPSTREAM_BASE}/api/free-ai/openai/chat"
UPSTREAM_MODEL = "mimo-auto"
MAX_OUTPUT_TOKENS = 128000
REFRESH_MARGIN = 300
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
    now = int(time.time() * 1000)
    encoded = ~(now * 0x1000) & 0xFFFFFFFFFFFF
    hex_part = encoded.to_bytes(6, "big").hex()
    return "ses_" + hex_part + _random_base62(14)


_SESSION_ID = _make_session_id()

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
