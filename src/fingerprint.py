#!/usr/bin/env python3
import hashlib
import os
import platform
import threading

_global_fp = None
_global_fp_lock = threading.Lock()
_GLOBAL_FP_FILE = "fp_global"


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


def _create_fp():
    hostname = platform.node()
    os_name = platform.system().lower()
    arch = _normalize_arch(platform.machine())
    cpu = _get_cpu_model()
    username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    raw = f"{hostname}|{os_name}|{arch}|{cpu}|{username}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _ensure_fp(fp_dir):
    global _global_fp
    if _global_fp:
        return _global_fp
    with _global_fp_lock:
        if _global_fp:
            return _global_fp
        fp_path = os.path.join(fp_dir, _GLOBAL_FP_FILE)
        try:
            with open(fp_path) as f:
                _global_fp = f.read().strip()
        except Exception:
            _global_fp = _create_fp()
            os.makedirs(fp_dir, exist_ok=True)
            with open(fp_path, "w") as f:
                f.write(_global_fp)
            os.chmod(fp_path, 0o600)
    return _global_fp
