#!/usr/bin/env python3
import base64
import json
import threading
import time
import urllib.error
import urllib.request

from . import constants
from . import fingerprint


class MimoBackend:
    def __init__(self, name, proxy_url, fingerprint_dir):
        self.name = name
        self.proxy_url = proxy_url
        self._fingerprint_dir = fingerprint_dir
        self.jwt = None
        self.jwt_exp = 0
        self._lock = threading.Lock()

    def _make_opener(self):
        if not self.proxy_url:
            return urllib.request.build_opener()
        return urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": self.proxy_url, "https": self.proxy_url}
            )
        )

    def _decode_exp(self, jwt):
        try:
            part = jwt.split(".")[1]
            pad = 4 - len(part) % 4
            if pad != 4:
                part += "=" * pad
            p = json.loads(base64.urlsafe_b64decode(part))
            if isinstance(p.get("exp"), (int, float)):
                return p["exp"] * 1000
        except Exception:
            pass
        return time.time() * 1000 + 3600 * 1000

    def _bootstrap(self):
        fp = fingerprint._ensure_fp(self._fingerprint_dir)
        body = json.dumps({"client": fp}).encode()
        req = urllib.request.Request(
            constants.BOOTSTRAP_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": constants.USER_AGENT},
        )
        with self._make_opener().open(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        jwt = data.get("jwt")
        if not jwt:
            raise RuntimeError(f"[{self.name}] bootstrap missing jwt")
        return jwt, self._decode_exp(jwt)

    def get_jwt(self, force=False):
        with self._lock:
            now = time.time() * 1000
            if (
                not force
                and self.jwt
                and (self.jwt_exp - now) > constants.REFRESH_MARGIN * 1000
            ):
                return self.jwt
            self.jwt, self.jwt_exp = self._bootstrap()
            constants.log(
                "INFO",
                f"JWT refreshed, exp in {int((self.jwt_exp - now) / 1000)}s",
                backend=self.name,
            )
            return self.jwt

    def start_jwt_refresher(self):
        def _run():
            while True:
                time.sleep(constants.JWT_REFRESH_INTERVAL)
                try:
                    self.get_jwt()
                except Exception as e:
                    constants.log("DEBUG", f"JWT refresher: {e}", backend=self.name)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def chat(self, payload, req_id=None):
        payload = dict(payload)
        payload["model"] = constants.UPSTREAM_MODEL
        payload["temperature"] = 1.0
        payload["max_tokens"] = constants.MAX_OUTPUT_TOKENS
        payload.pop("top_p", None)
        payload.pop("top_k", None)

        msgs = constants._MIMO_PREFIX_MESSAGES + list(payload.get("messages") or [])
        payload["messages"] = msgs

        opener = self._make_opener()

        def _do(jwt):
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                constants.CHAT_URL,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "X-Mimo-Source": "mimocode-cli-free",
                    "Content-Type": "application/json",
                    "User-Agent": constants.USER_AGENT,
                    "x-session-affinity": constants._SESSION_ID,
                },
            )
            return opener.open(req, timeout=300)

        try:
            return _do(self.get_jwt())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                constants.log("WARN", f"got {e.code} -> refresh JWT retry",
                    req_id=req_id, backend=self.name)
                return _do(self.get_jwt(force=True))
            if e.code == 441:
                constants.log("WARN", "upstream 441 risk control blocked",
                    req_id=req_id, backend=self.name)
            raise


class RoundRobin:
    def __init__(self, backends):
        self._backends = backends
        self._i = 0
        self._lock = threading.Lock()

    def __len__(self):
        return len(self._backends)

    def pick(self):
        with self._lock:
            b = self._backends[self._i]
            self._i = (self._i + 1) % len(self._backends)
            return b
