#!/usr/bin/env python3
"""per-backend 设备指纹：SHA256(hostname|os|arch|cpu|username)，与 mimo-free 扩展一致"""
import hashlib
import os
import platform


def _get_cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def _normalize_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("x86_64", "amd64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def create_fingerprint():
    hostname = platform.node()
    os_name = platform.system().lower()
    arch = _normalize_arch(platform.machine())
    cpu = _get_cpu_model()
    username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    raw = f"{hostname}|{os_name}|{arch}|{cpu}|{username}"
    return hashlib.sha256(raw.encode()).hexdigest()


def load_or_create_fingerprint(fp_dir, name):
    os.makedirs(fp_dir, exist_ok=True)
    fp_path = os.path.join(fp_dir, f"fp_{name}")
    try:
        with open(fp_path) as f:
            return f.read().strip()
    except Exception:
        fp = create_fingerprint()
        with open(fp_path, "w") as f:
            f.write(fp)
        os.chmod(fp_path, 0o600)
        return fp
