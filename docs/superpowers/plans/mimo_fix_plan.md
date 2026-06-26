# MiMo Proxy 修复 — 实现计划

> 日期：2026-06-27
> 来源：docs/mimo_gap_analysis.md

## 改动范围

只改 `mimo_code_proxy.py`，不动测试。

## 实施顺序

### P0 — 致命缺陷（3 个子任务，顺序不能乱）

#### 1. 指纹格式对齐 + 全局单指纹

**改 `_create_fp()`（约第 90-103 行）**

```python
# 当前（6字段，含 random）：
raw = "|".join([self.name, platform.node(), "linux", "x64",
                 platform.processor() or "x86_64", str(random.random())])

# 改为（5字段，全局单指纹，格式同 Go 版）：
hostname = platform.node()
os_name = platform.system()
arch = platform.machine()
username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
uid = str(uuid.uuid4())
raw = f"{hostname}|{os_name}|{arch}|{username}|{uid}"
```

**改 `__init__`（约第 65-69 行）**：指纹改为类级别共享（`_fp_lock` 从实例锁改为类锁或模块级 `threading.Lock` + `_global_fp`）。

**具体做法**：在模块级别加 `_global_fp = None` 和 `_global_fp_lock = threading.Lock()`，`ensure_fp()` 改为检查全局指纹，首次生成后所有 backend 复用。

指纹文件路径改用固定名 `fp_global` 而非 `fp_{name}`。

#### 2. 请求头补齐 — bootstrap

**改 `_bootstrap()`（约第 130-144 行）**

```python
# 当前：
headers={"Content-Type": "application/json"}

# 改为：
headers={
    "Content-Type": "application/json",
    "User-Agent": "mimocode/1.0.0",
}
```

#### 3. 请求头补齐 — chat

**改 `_do()`（约第 201-213 行）**

```python
# 当前：
headers={
    "Authorization": f"Bearer {jwt}",
    "X-Mimo-Source": "mimocode-cli-free",
    "Content-Type": "application/json",
}

# 改为：
headers={
    "Authorization": f"Bearer {jwt}",
    "X-Mimo-Source": "mimocode-cli-free",
    "x-session-affinity": "ses_" + random_hex(12),
    "User-Agent": "mimocode/1.0.0",
    "Content-Type": "application/json",
}
```

需要加一个 `random_hex(n)` 辅助函数（secrets.token_hex）。

---

### P1 — 错误处理 + CORS（2 个子任务）

#### 4. 441 风控错误

**改 `chat()` 的 HTTPError 处理（约第 217-236 行）**：在 `e.code == 429` 分支**前面**插入：

```python
if e.code == 441:
    log("WARN", f"upstream 441 risk control blocked", req_id=req_id, backend=self.name)
    raise  # 透传，不重试
```

#### 5. CORS 支持

**改 `make_handler()` 内的 `do_OPTIONS` / 响应头**：在每个请求处理中加 CORS headers：

```python
@staticmethod
def _set_cors(handler):
    handler.send_response(200 if handler.command == "OPTIONS" else ...)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
```

---

### P2 — 可观测性（2 个子任务）

#### 6. JWT 后台自动刷新

**改 `MimoBackend`**：加一个类方法启动后台线程，每 30s 检查 JWT 过期时间，提前刷新。

```python
def start_jwt_refresher(self):
    """后台线程，每 30s 检查并刷新 JWT"""
    def _run():
        while True:
            time.sleep(30)
            try:
                self.get_jwt()  # 内部会检查是否快过期
            except Exception:
                pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()
```

在 `main()` 里每个 backend 初始化后调用 `be.start_jwt_refresher()`。

#### 7. 请求统计 + health 增强

**改 `make_handler()`**：加 `_req_count = {}`（dict），`do_POST` 时 `_req_count[be.name] += 1`。
**改 `/health` 响应**：返回 `{"status": "ok", "backends": 10, "requests": {"kr-01": 5, ...}}`

---

#### 8. 请求日志

**改 `_log_req()`**：每个请求结束时 INFO 输出 `status=N elapsed=Nms`。

---

## 测试验证

每个 phase 结束后运行：

```bash
source .venv/bin/activate && python3 -m unittest test_mimo_code_proxy -v
```

47 测试必须全过。

P0 完成后额外验证：启动服务发 chat，应不再返回 403。

---

## 改动量估算

| Phase | 行数（增/删） | 文件 |
|-------|-------------|------|
| P0 | ~30 / ~15 | mimo_code_proxy.py |
| P1 | ~20 / ~2 | mimo_code_proxy.py |
| P2 | ~30 / ~0 | mimo_code_proxy.py |
| **合计** | **~80 / ~17** | 1 文件 |
