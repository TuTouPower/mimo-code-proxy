#!/usr/bin/env python3
import json
import os


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("listen", {})
    cfg["listen"].setdefault("host", "0.0.0.0")
    cfg["listen"].setdefault("port", 8788)
    cfg.setdefault("api_key", "")
    cfg.setdefault(
        "fingerprint_dir", "/data/mimo_fingerprints",
    )
    backends = cfg.get("backends", [])
    if not backends:
        raise ValueError("配置文件中没有 backend")
    for b in backends:
        if "name" not in b:
            raise ValueError("每个 backend 必须包含 'name'")
        b.setdefault("proxy", None)
    return cfg
