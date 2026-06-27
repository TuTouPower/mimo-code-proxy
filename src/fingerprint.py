#!/usr/bin/env python3
"""per-backend 设备指纹：SHA256(hostname|os|arch|cpu|username)，与 mimo-free 扩展一致"""
import hashlib
import os

_CPU_POOL = [
    "Intel(R) Core(TM) i7-14700K",
    "Intel(R) Core(TM) i9-14900K",
    "Intel(R) Core(TM) i5-14600K",
    "Intel(R) Core(TM) i7-13700K",
    "Intel(R) Core(TM) i9-13900K",
    "Intel(R) Core(TM) i5-13600K",
    "AMD Ryzen 9 7950X",
    "AMD Ryzen 7 7800X3D",
    "AMD Ryzen 5 7600X",
    "Intel(R) Core(TM) Ultra 9 285K",
    "Intel(R) Core(TM) Ultra 7 265K",
    "AMD Ryzen 9 9950X",
]


def _pick_cpu(name: str) -> str:
    i = sum(ord(c) for c in name) % len(_CPU_POOL)
    return _CPU_POOL[i]


def create_fingerprint(name: str = "") -> str:
    cpu = _pick_cpu(name) if name else "unknown"
    raw = f"{name}|linux|x64|{cpu}|{name or 'unknown'}"
    return hashlib.sha256(raw.encode()).hexdigest()


def load_or_create_fingerprint(fp_dir, name):
    os.makedirs(fp_dir, exist_ok=True)
    fp_path = os.path.join(fp_dir, f"fp_{name}")
    try:
        with open(fp_path) as f:
            return f.read().strip()
    except Exception:
        fp = create_fingerprint(name)
        with open(fp_path, "w") as f:
            f.write(fp)
        os.chmod(fp_path, 0o600)
        return fp
