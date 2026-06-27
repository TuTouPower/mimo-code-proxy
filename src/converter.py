#!/usr/bin/env python3
import json
import uuid

from . import constants


def anthropic_to_openai(req):
    oai = {"model": req.get("model", constants.UPSTREAM_MODEL)}
    if req.get("stream"):
        oai["stream"] = True
    if "max_tokens" in req:
        oai["max_tokens"] = req["max_tokens"]
    if "temperature" in req:
        oai["temperature"] = req["temperature"]
    if "top_p" in req:
        oai["top_p"] = req["top_p"]
    if "tools" in req:
        oai["tools"] = req["tools"]
    if "tool_choice" in req:
        oai["tool_choice"] = req["tool_choice"]
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


def openai_sse_to_anthropic_sse_line(oai_chunk_line):
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
        events.append({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    elif "content" in delta and delta["content"]:
        events.append({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta["content"]}})
    if finish:
        events.append({"type": "content_block_stop", "index": 0})
        events.append({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 0}})
        events.append({"type": "message_stop"})
    return events
