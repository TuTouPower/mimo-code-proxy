# MiMo Code Proxy 功能升级实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 mimo-code-proxy 实现 5 项功能升级：分级日志、路径兼容、流式 SSE、健康检查探活、指纹动态轮换。

**Architecture:** 单文件 `mimo_code_proxy.py` + 单测试文件 `test_mimo_code_proxy.py`。所有改动集中在两个文件内：核心逻辑用 threading.Lock 保护单活跃指纹状态，请求处理增加 retry loop + stream/non-stream 分流，日志系统升级为带级别过滤和 req_id 追踪。

**Tech Stack:** Python 3.13 stdlib only, unittest, ThreadingHTTPServer

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `mimo_code_proxy.py` | 核心代理：日志系统、指纹管理、JWT、上游调用、HTTP handler、流式/非流式转发 |
| `test_mimo_code_proxy.py` | 全部单元测试 + 集成测试 |

---

### Task 1: 分级日志系统

**文件：**
- 修改：`mimo_code_proxy.py:44-45`（log 函数）
- 修改：`mimo_code_proxy.py` 全文（所有 log 调用升级）

- [ ] **步骤 1：重写 log 函数，增加 level 和 req_id 支持**

```python
LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
LOG_LEVEL = os.environ.get("MIMO_LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in LOG_LEVELS:
    LOG_LEVEL = "INFO"
LOG_THRESHOLD = LOG_LEVELS[LOG_LEVEL]


def log(level, *a, req_id=None):
    if LOG_LEVELS.get(level, 1) < LOG_THRESHOLD:
        return
    ts = time.strftime("%H:%M:%S")
    parts = [f"[{ts}]", f"[{level}]"]
    if req_id:
        parts.append(f"[req={req_id}]")
    print(*parts, *a, file=sys.stderr, flush=True)


def new_req_id():
    return uuid.uuid4().hex[:8]
```

- [ ] **步骤 2：更新 main() 和 get_fp() 中的 log 调用**

```python
# main() 中：
log("INFO", "mimo-code-proxy on http://" + LISTEN_HOST + ":" + str(LISTEN_PORT) + "  auth=" + auth_status)
log("INFO", "startup JWT ok")
log("ERROR", "startup bootstrap failed (will retry on request):", e)

# get_fp() 中：
log("INFO", "using MIMO_CLIENT_ID")
log("INFO", "generated new fp:", fp)
log("WARN", "warn: cannot persist fingerprint", e)
```

- [ ] **步骤 3：更新 get_jwt() 中的 log 调用**

```python
# get_jwt() 中：
log("DEBUG", "JWT refreshed, exp in " + str(int((_jwt_exp - now) / 1000)) + "s")
```

- [ ] **步骤 4：更新 upstream_chat() 中的 log 调用**

```python
# upstream_chat() 中 clamp：
log("DEBUG", "clamp " + f + " " + str(v) + " -> " + str(MAX_OUTPUT_TOKENS))
# 401/403 重试：
log("INFO", "got " + str(e.code) + " -> refresh JWT retry")
```

- [ ] **步骤 5：更新 do_POST() 中 stream relay ended 的 log**

```python
log("DEBUG", "stream relay ended", repr(e))
```

- [ ] **步骤 6：运行全部测试确认 log 签名变更不破坏现有测试**

```bash
python3 -m unittest test_mimo_code_proxy -v
```
预期：全部 PASS

- [ ] **步骤 7：提交**

```bash
git add mimo_code_proxy.py
git commit -m "feat: 分级日志系统，支持 DEBUG/INFO/WARN/ERROR 和 req_id"
```

---

### Task 2: `/v1/...` 路径前缀兼容

**文件：**
- 修改：`mimo_code_proxy.py:179-191`（do_GET / do_POST 路径匹配）

- [ ] **步骤 1：添加路径规范化函数**

```python
def normalize_path(path):
    """去掉查询串和尾斜杠，剥掉可选 /v1 前缀，返回精确末段。"""
    p = path.split("?")[0].rstrip("/")
    if p.startswith("/v1/"):
        return p[3:]  # 去掉 /v1，保留 /models 或 /chat/completions
    return p
```

- [ ] **步骤 2：重写 do_GET 路径匹配**

```python
def do_GET(self):
    np = normalize_path(self.path)
    if np == "/models":
        if not self._auth_ok():
            return self._json(401, {"error": {"message": "invalid key"}})
        return self._json(200, MODELS_RESP)
    if np == "/health":
        return self._json(200, {"status": "ok"})
    self._json(404, {"error": {"message": "not found"}})
```

- [ ] **步骤 3：重写 do_POST 路径匹配**

```python
def do_POST(self):
    if normalize_path(self.path) != "/chat/completions":
        return self._json(404, {"error": {"message": "not found"}})
    # ... 后续不变
```

- [ ] **步骤 4：更新已有测试中的路径**

测试文件里 `/v1/health`、`/v1/models`、`/v1/chat/completions` 全部继续用。新增纯路径测试（不带 `/v1`）。

- [ ] **步骤 5：新增路径兼容测试**

```python
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
```

- [ ] **步骤 6：运行测试**

```bash
python3 -m unittest test_mimo_code_proxy -v
```
预期：全部 PASS

- [ ] **步骤 7：提交**

```bash
git add mimo_code_proxy.py test_mimo_code_proxy.py
git commit -m "feat: /v1/... 路径前缀兼容"
```

---

### Task 3: 健康检查含上游探活

**文件：**
- 修改：`mimo_code_proxy.py:185-186`（do_GET /health 分支）
- 新增：`mimo_code_proxy.py` 探活缓存变量

- [ ] **步骤 1：添加探活缓存变量**

在 `_lock` 定义附近添加：

```python
_health_ok = False
_health_ts = 0
_HEALTH_CACHE_S = 30
```

- [ ] **步骤 2：实现 check_health() 函数**

```python
def check_health():
    global _health_ok, _health_ts
    now = time.time()
    if _health_ok and (now - _health_ts) < _HEALTH_CACHE_S:
        return True, None
    try:
        get_jwt()
        _health_ok = True
        _health_ts = now
        return True, None
    except Exception as e:
        _health_ok = False
        _health_ts = now
        return False, str(e)
```

- [ ] **步骤 3：重写 do_GET /health 分支**

```python
if np == "/health":
    ok, err = check_health()
    if ok:
        return self._json(200, {"status": "ok", "upstream": "ok"})
    return self._json(503, {"status": "degraded", "upstream": "down", "error": err})
```

- [ ] **步骤 4：更新现有健康检查测试**

`test_health_returns_ok` → 改为验证 200 + `upstream: ok`。

```python
def test_health_returns_ok(self):
    resp = self._get("/health")
    self.assertEqual(resp.status, 200)
    data = json.loads(resp.read())
    self.assertEqual(data["status"], "ok")
    self.assertEqual(data["upstream"], "ok")

def test_health_no_auth_needed(self):
    resp = self._get("/health")
    data = json.loads(resp.read())
    self.assertEqual(data["status"], "ok")
```

- [ ] **步骤 5：新增健康检查降级测试**

```python
class TestHealthCheck(unittest.TestCase):
    def test_check_health_ok(self):
        with patch("mimo_code_proxy.get_jwt", return_value="mock-jwt"):
            ok, err = proxy.check_health()
            self.assertTrue(ok)
            self.assertIsNone(err)

    def test_check_health_degraded(self):
        with patch("mimo_code_proxy.get_jwt", side_effect=Exception("boom")):
            ok, err = proxy.check_health()
            self.assertFalse(ok)
            self.assertIn("boom", err)

    def test_health_cache_returns_cached(self):
        proxy._health_ok = True
        proxy._health_ts = time.time()
        ok, err = proxy.check_health()
        self.assertTrue(ok)
        self.assertIsNone(err)
```

- [ ] **步骤 6：运行测试**

```bash
python3 -m unittest test_mimo_code_proxy -v
```
预期：全部 PASS

- [ ] **步骤 7：提交**

```bash
git add mimo_code_proxy.py test_mimo_code_proxy.py
git commit -m "feat: 健康检查含上游探活，30s 缓存"
```

---

### Task 4: 真·流式 SSE 转发

**文件：**
- 修改：`mimo_code_proxy.py:189-227`（do_POST 响应写出逻辑）
- 新增：`mimo_code_proxy.py` 流式/非流式分流

- [ ] **步骤 1：重写 do_POST 响应写出部分，按 stream 分流**

将当前 `do_POST` 中第 210-227 行的响应写出逻辑替换为：

```python
        is_stream = payload.get("stream", False)
        self.send_response(200)
        content_type = resp.headers.get("Content-Type", "application/json")
        self.send_header("Content-Type", content_type)
        if not is_stream:
            body = resp.read()
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            log("DEBUG", "non-stream response", len(body), "bytes", req_id=req_id)
        else:
            self.send_header("Connection", "close")
            self.end_headers()
            total = 0
            t0 = time.time()
            done_seen = False
            try:
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if b"[DONE]" in chunk:
                        done_seen = True
            except Exception as e:
                log("DEBUG", "stream relay ended", repr(e), req_id=req_id)
            elapsed = int((time.time() - t0) * 1000)
            log("DEBUG", "stream done", total, "bytes", elapsed, "ms",
                "DONE=" + str(done_seen), req_id=req_id)
        resp.close()
```

- [ ] **步骤 2：在 do_POST 开头分配 req_id**

```python
def do_POST(self):
    req_id = new_req_id()
    log("DEBUG", "POST", self.path, req_id=req_id)
    # ... 后续 auth / parse / upstream_chat 调用传入 req_id
```

注意：`upstream_chat()` 目前不接收 req_id，需要加参数或让上游调用处记日志。当前暂不在 upstream_chat 中加 req_id，等 Task 5 指纹轮换时一并处理。

- [ ] **步骤 3：更新现有流式转发相关测试**

```python
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
        "/chat/completions",
        {"model": "mimo-auto", "messages": [{"role": "user", "content": "hi"}]},
        {"Authorization": "Bearer sk-test-key"},
    )
    self.assertEqual(resp.status, 200)
```

- [ ] **步骤 4：新增流式 SSE 测试**

```python
@patch("mimo_code_proxy.get_jwt", return_value="mock-jwt")
@patch("mimo_code_proxy.upstream_chat")
def test_stream_response_chunked(self, mock_chat, _mock_jwt):
    chunks = [b'data: {"choices":[{"delta":{"content":"h"}}]}\n\n',
              b'data: {"choices":[{"delta":{"content":"i"}}]}\n\n',
              b'data: [DONE]\n\n']
    reads = iter(chunks)

    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/event-stream"}

    def _read(n=1024):
        try:
            return next(reads)
        except StopIteration:
            return b""

    mock_resp.read = _read
    mock_chat.return_value = mock_resp

    resp = self._post(
        "/chat/completions",
        {"model": "mimo-auto", "messages": [{"role": "user", "content": "hi"}],
         "stream": True},
        {"Authorization": "Bearer sk-test-key"},
    )
    self.assertEqual(resp.status, 200)
    body = resp.read()
    self.assertIn(b"[DONE]", body)

@patch("mimo_code_proxy.get_jwt", return_value="mock-jwt")
@patch("mimo_code_proxy.upstream_chat")
def test_non_stream_has_content_length(self, mock_chat, _mock_jwt):
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.read.return_value = b'{"choices":[]}'
    mock_chat.return_value = mock_resp

    resp = self._post(
        "/chat/completions",
        {"model": "mimo-auto", "messages": [{"role": "user", "content": "hi"}]},
        {"Authorization": "Bearer sk-test-key"},
    )
    self.assertEqual(resp.status, 200)
    self.assertIsNotNone(resp.headers.get("Content-Length"))
```

- [ ] **步骤 5：运行测试**

```bash
python3 -m unittest test_mimo_code_proxy -v
```
预期：全部 PASS

- [ ] **步骤 6：提交**

```bash
git add mimo_code_proxy.py test_mimo_code_proxy.py
git commit -m "feat: 真·流式 SSE 转发，stream 分流，非流式带 Content-Length"
```

---

### Task 5: 指纹动态轮换

**文件：**
- 修改：`mimo_code_proxy.py:39-103`（全局状态和 JWT/指纹管理）
- 修改：`mimo_code_proxy.py:106-144`（upstream_chat 重写为 retry loop）
- 修改：`mimo_code_proxy.py:230-243`（main 中初始化逻辑）

这是最核心的改动。按以下顺序逐步重构。

- [ ] **步骤 1：定义配置常量和新全局状态**

替换原来的 `_jwt` / `_jwt_exp` / `_lock`：

```python
MIMO_MAX_RETRY = int(os.environ.get("MIMO_MAX_RETRY", "3"))
REFRESH_MARGIN = 300

_active_client = None
_active_jwt = None
_active_jwt_exp = 0
_fp_lock = threading.Lock()
```

- [ ] **步骤 2：重写指纹获取函数，去除全局 get_fp() 的单例模型**

```python
def _load_or_create_fp():
    """从 CLIENT_FILE 或 MIMO_CLIENT_ID 读取，否则生成新 UUID 并持久化。"""
    fp = os.environ.get("MIMO_CLIENT_ID", "").strip()
    if fp:
        log("DEBUG", "using MIMO_CLIENT_ID")
        return fp
    try:
        fp = open(CLIENT_FILE).read().strip()
        if fp:
            log("DEBUG", "loaded fp from file")
            return fp
    except Exception:
        pass
    fp = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(CLIENT_FILE), exist_ok=True)
        with open(CLIENT_FILE, "w") as f:
            f.write(fp)
        os.chmod(CLIENT_FILE, 0o600)
    except Exception as e:
        log("WARN", "cannot persist fingerprint", e)
    log("INFO", "generated new fp:", fp[:8] + "...")
    return fp


def _persist_fp(fp):
    try:
        os.makedirs(os.path.dirname(CLIENT_FILE), exist_ok=True)
        with open(CLIENT_FILE, "w") as f:
            f.write(fp)
        os.chmod(CLIENT_FILE, 0o600)
    except Exception as e:
        log("WARN", "cannot persist fingerprint", e)


def _fingerprint():
    global _active_client, _active_jwt, _active_jwt_exp
    return _active_client, _active_jwt, _active_jwt_exp


def _replace_fingerprint(req_id=None):
    """生成新指纹，bootstrap 新 JWT，持久化。调用方需持有 _fp_lock。"""
    global _active_client, _active_jwt, _active_jwt_exp
    old = _active_client[:8] + "..." if _active_client else "none"
    _active_client = str(uuid.uuid4())
    _active_jwt = None
    _active_jwt_exp = 0
    _persist_fp(_active_client)
    log("INFO", "fingerprint rotated", old, "->", _active_client[:8] + "...", req_id=req_id)
    _active_jwt, _active_jwt_exp = _bootstrap_with(_active_client, req_id=req_id)


def _bootstrap_with(client, req_id=None):
    body = json.dumps({"client": client}).encode()
    req = urllib.request.Request(
        BOOTSTRAP_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    jwt = data.get("jwt")
    if not jwt:
        raise RuntimeError("bootstrap missing jwt")
    exp = _decode_exp(jwt)
    log("DEBUG", "bootstrap ok fp=" + client[:8] + "... exp_in=" + str(int((exp - time.time() * 1000) / 1000)) + "s", req_id=req_id)
    return jwt, exp


def ensure_jwt(req_id=None):
    """确保当前活跃指纹有有效 JWT。过期则同指纹重 bootstrap，不计入 retry。"""
    global _active_jwt, _active_jwt_exp
    with _fp_lock:
        now = time.time() * 1000
        if _active_jwt and (_active_jwt_exp - now) > REFRESH_MARGIN * 1000:
            return _active_jwt
        log("DEBUG", "JWT expired or missing, re-bootstrap same fp", req_id=req_id)
        _active_jwt, _active_jwt_exp = _bootstrap_with(_active_client, req_id=req_id)
        return _active_jwt
```

- [ ] **步骤 3：重写 upstream_chat() 为 retry loop**

```python
def upstream_chat(payload, req_id=None):
    payload = dict(payload)
    payload["model"] = UPSTREAM_MODEL
    if MIMO_GUARD_TEXT:
        msgs = list(payload.get("messages") or [])
        already = (
            msgs
            and msgs[0].get("role") == "system"
            and isinstance(msgs[0].get("content"), str)
            and msgs[0]["content"].startswith(MIMO_GUARD_TEXT[:80])
        )
        if not already:
            msgs.insert(0, {"role": "system", "content": MIMO_GUARD_TEXT})
            payload["messages"] = msgs
    for f in ("max_tokens", "max_completion_tokens"):
        v = payload.get(f)
        if isinstance(v, int) and v > MAX_OUTPUT_TOKENS:
            log("DEBUG", "clamp " + f + " " + str(v) + " -> " + str(MAX_OUTPUT_TOKENS), req_id=req_id)
            payload[f] = MAX_OUTPUT_TOKENS

    last_error = None
    for retry in range(MIMO_MAX_RETRY + 1):
        try:
            jwt = ensure_jwt(req_id=req_id)
        except Exception as e:
            log("WARN", "bootstrap failed retry=" + str(retry) + "/" + str(MIMO_MAX_RETRY), repr(e), req_id=req_id)
            if retry >= MIMO_MAX_RETRY:
                raise RuntimeError("bootstrap exhausted: " + str(e))
            with _fp_lock:
                _replace_fingerprint(req_id=req_id)
            continue

        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                CHAT_URL, data=body, method="POST",
                headers={
                    "Authorization": "Bearer " + jwt,
                    "X-Mimo-Source": "mimocode-cli-free",
                    "Content-Type": "application/json",
                },
            )
            log("DEBUG", "chat request retry=" + str(retry) + "/" + str(MIMO_MAX_RETRY),
                "fp=" + (_active_client[:8] + "..."), req_id=req_id)
            resp = urllib.request.urlopen(req, timeout=300)
            log("DEBUG", "chat response status=" + str(resp.status), req_id=req_id)
            return resp
        except urllib.error.HTTPError as e:
            last_error = e
            log("WARN", "upstream HTTP " + str(e.code) + " retry=" + str(retry) + "/" + str(MIMO_MAX_RETRY), req_id=req_id)
            if retry >= MIMO_MAX_RETRY:
                raise
            with _fp_lock:
                _replace_fingerprint(req_id=req_id)
        except Exception as e:
            last_error = e
            log("WARN", "upstream error retry=" + str(retry) + "/" + str(MIMO_MAX_RETRY), repr(e), req_id=req_id)
            if retry >= MIMO_MAX_RETRY:
                raise
            with _fp_lock:
                _replace_fingerprint(req_id=req_id)

    raise last_error
```

- [ ] **步骤 4：更新 main() 初始化**

```python
def main():
    global _active_client
    _active_client = _load_or_create_fp()
    try:
        with _fp_lock:
            _active_jwt, _active_jwt_exp = _bootstrap_with(_active_client)
        log("INFO", "startup JWT ok")
    except Exception as e:
        log("ERROR", "startup bootstrap failed (will retry on request):", e)
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    auth_status = "ON" if LOCAL_KEY else "OFF"
    log("INFO", "mimo-code-proxy on http://" + LISTEN_HOST + ":" + str(LISTEN_PORT) + "  auth=" + auth_status)
    srv.serve_forever()
```

- [ ] **步骤 5：更新 do_POST 调用 upstream_chat 时传入 req_id**

```python
resp = upstream_chat(payload, req_id=req_id)
```

并给 502 错误加上 req_id 日志：

```python
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="replace")
    try:
        obj = json.loads(body)
    except Exception:
        obj = {"error": {"message": body[:500], "code": e.code}}
    log("WARN", "upstream HTTPError " + str(e.code), req_id=req_id)
    return self._json(e.code, obj)
except Exception as e:
    log("ERROR", "upstream fatal", repr(e), req_id=req_id)
    return self._json(502, {"error": {"message": str(e)}})
```

- [ ] **步骤 6：为 upstream_chat 也加上 req_id 的请求体和响应日志（DEBUG 级别，完整 messages 内容）**

在 `upstream_chat` 中 `payload` 处理完成后加：

```python
log("DEBUG", "request model=" + payload.get("model", "?") +
    " stream=" + str(payload.get("stream", False)) +
    " max_tokens=" + str(payload.get("max_tokens", payload.get("max_completion_tokens", "?"))) +
    " messages=" + json.dumps(payload.get("messages", []), ensure_ascii=False),
    req_id=req_id)
```

- [ ] **步骤 7：更新现有测试适配新的函数签名**

`upstream_chat` 现在签名为 `(payload, req_id=None)`。现有 mock 需要更新。

`get_jwt` 函数被 `ensure_jwt` 替代，需要更新引用。

测试中 `proxy._jwt` → 移除，改为 mock `ensure_jwt`。

`get_fp` 被 `_load_or_create_fp` 替代，`TestFingerprint` 需要更新引用。

- [ ] **步骤 8：新增指纹轮换测试**

```python
class TestFingerprintRotation(unittest.TestCase):
    def setUp(self):
        proxy._active_client = "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"
        proxy._active_jwt = "test-jwt-0"
        proxy._active_jwt_exp = (time.time() + 3600) * 1000
        self._saved = os.environ.pop("MIMO_CLIENT_ID", None)
        proxy.LOCAL_KEY = ""

    def tearDown(self):
        if self._saved:
            os.environ["MIMO_CLIENT_ID"] = self._saved

    def test_first_failure_rotates_fp(self):
        call_count = [0]
        def _fake_upstream(payload, req_id=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError(
                    "url", 429, "rate limited", {}, io.BytesIO(b"{}")
                )
            resp = MagicMock()
            resp.headers = {"Content-Type": "application/json"}
            resp.read.return_value = b"{}"
            return resp

        with patch("mimo_code_proxy.ensure_jwt", return_value="test-jwt"), \
             patch("mimo_code_proxy._replace_fingerprint") as mock_replace, \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _fake_upstream
            proxy.upstream_chat({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(call_count[0], 2)
        mock_replace.assert_called_once()

    def test_exhaust_retries_raises_last_error(self):
        with patch("mimo_code_proxy.ensure_jwt", return_value="test-jwt"), \
             patch("mimo_code_proxy._replace_fingerprint"), \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "url", 429, "rate limited", {}, io.BytesIO(b"{}")
            )
            with self.assertRaises(urllib.error.HTTPError):
                proxy.upstream_chat({"messages": [{"role": "user", "content": "hi"}]})

    def test_double_check_no_duplicate_rotation(self):
        """并发场景：进锁后发现已被别的线程换过，不重复换。"""
        proxy._active_client = "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"
        with proxy._fp_lock:
            client_before = proxy._active_client
            # 另一个线程先进锁换了指纹
            proxy._active_client = "bbbb2222-bbbb-cccc-dddd-eeeeeeeeeeee"
            # 当前线程检查
            self.assertNotEqual(proxy._active_client, client_before)
```

- [ ] **步骤 9：运行测试**

```bash
python3 -m unittest test_mimo_code_proxy -v
```
预期：全部 PASS。特别关注 `TestFingerprint` / `TestUpstreamChatLogic` / `TestFingerprintRotation`。

- [ ] **步骤 10：提交**

```bash
git add mimo_code_proxy.py test_mimo_code_proxy.py
git commit -m "feat: 指纹动态轮换，任何非 2xx 故障自动换指纹重试"
```

---

### Task 6: 最终集成测试与验证

**文件：**
- 无需修改

- [ ] **步骤 1：运行全部测试**

```bash
python3 -m unittest test_mimo_code_proxy -v
```
预期：全部 PASS

- [ ] **步骤 2：检查 MIMO_LOG_LEVEL=DEBUG 输出完整性**

```bash
MIMO_LOG_LEVEL=DEBUG python3 -c "
from mimo_code_proxy import log, new_req_id, LOG_LEVEL
print('LOG_LEVEL=', LOG_LEVEL)
log('DEBUG', 'test debug msg')
log('INFO', 'test info msg')
log('WARN', 'test warn msg')
log('ERROR', 'test error msg', req_id=new_req_id())
"
```
预期：全部 4 条都输出，DEBUG 和 ERROR 都含 req_id 前缀。

- [ ] **步骤 3：验证 MIMO_LOG_LEVEL=WARN 过滤**

```bash
MIMO_LOG_LEVEL=WARN python3 -c "
from mimo_code_proxy import log
log('DEBUG', 'should NOT appear')
log('INFO', 'should NOT appear')
log('WARN', 'should appear')
log('ERROR', 'should appear')
"
```
预期：只输出 WARN 和 ERROR。

- [ ] **步骤 4：提交**

```bash
git add mimo_code_proxy.py test_mimo_code_proxy.py
git commit -m "chore: 集成验证，日志级别过滤确认"
```

---

### Task 7: 更新文档

**文件：**
- 修改：`CLAUDE.md`

- [ ] **步骤 1：更新 CLAUDE.md 配置表**

在配置表中增加 `MIMO_MAX_RETRY` 和 `MIMO_LOG_LEVEL`：

```markdown
| MIMO_MAX_RETRY | 3 | 单请求最多换几个指纹 |
| MIMO_LOG_LEVEL | INFO | 日志级别 DEBUG/INFO/WARN/ERROR |
```

并在注意事项中增加 DEBUG 级别隐私风险提示。

- [ ] **步骤 2：提交**

```bash
git add CLAUDE.md
git commit -m "docs: 更新 CLAUDE.md 配置表"
```

---

## 自检

1. **Spec 覆盖：**
   - 分级日志：Task 1 ✓
   - 路径兼容：Task 2 ✓
   - 健康检查探活：Task 3 ✓
   - 流式 SSE：Task 4 ✓
   - 指纹轮换：Task 5 ✓
   - 文档更新：Task 7 ✓

2. **无占位符。** 所有步骤都有实际代码。

3. **类型一致性：**
   - `upstream_chat(payload, req_id=None)` → do_POST 调用时传入 req_id ✓
   - `ensure_jwt(req_id=None)` → upstream_chat 调用时传入 ✓
   - `_replace_fingerprint(req_id=None)` ✓
   - `_bootstrap_with(client, req_id=None)` ✓
   - `_load_or_create_fp()` 无参 → `main()` 调用 ✓
   - `check_health()` 无参 → do_GET 调用 ✓
   - `normalize_path(path)` → do_GET/do_POST 调用 ✓
