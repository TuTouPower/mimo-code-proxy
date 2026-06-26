# MiMo Proxy 缺陷分析

> 基于 docs/1.md（逆向文档）、vendors/mimo-proxy（Go 参考实现）、实测结果，梳理当前 Python 版全部差距。

**日期：** 2026-06-27

---

## 一、致命缺陷（直接导致 403）

### 1.1 缺 `x-session-affinity` header

| | 文档要求 | 当前代码 |
|---|---------|---------|
| 状态 | :white_check_mark: 必须 | **缺失** |
| 值 | `ses_<随机12字节hex>` | - |
| 位置 | chat 请求 header | - |

Go 版第 263 行：
```go
req.Header.Set("x-session-affinity", "ses_"+randomHex(12))
```

**这是 403 Illegal access 的最可能根因。** 文档 4.1 节明确标为必须。

### 1.2 缺 `User-Agent` header

| | Go 版 | 当前代码 |
|---|------|---------|
| bootstrap | `mimocode/1.0.0` | 无 |
| chat | `mimocode/1.0.0` | 无 |

Go 版第 186 行（bootstrap）和第 264 行（chat）都设了 `User-Agent: mimocode/1.0.0`。Python 版的 `urllib` 会发默认 UA 如 `Python-urllib/3.x`，可能被上游识别为非 CLI 请求而拒绝。

---

## 二、指纹格式错误

### 2.1 字段数量和顺序不对

| 序号 | 文档格式 | Go 版 | Python 版 |
|------|---------|-------|----------|
| 1 | `hostname` | `hostname` | `self.name`（backend 名）|
| 2 | `os_name` | `GOOS` | 硬编码 `"linux"` |
| 3 | `architecture` | `GOARCH` | 硬编码 `"x64"` |
| 4 | `cpu_model` | `username` | `platform.processor()` |
| 5 | `username` | `randomUUID()` | `str(random.random())` |
| 6 | - | - | `str(random.random())`（多余）|

### 2.2 关键问题

| 问题 | 影响 |
|------|------|
| `str(random.random())` 每次生成不同指纹 | 同机无法复用，每次都要新 bootstrap |
| `self.name` 代替 hostname | Docker 容器内 fingerprint 不反映真实机器 |
| 硬编码 OS/arch | WSL 和 Linux 服务器指纹格式不同 |
| 6 字段 vs 5 字段 | 指纹格式与 CLI 不一致 |

### 2.3 多指纹 vs 单指纹

Go 版：全局**单指纹**，`sync.Once` 确保全服务共用一个。
Python 版：每个 backend（10 个）独立指纹。

10 个不同指纹从同一个 IP 请求 → 上游视角像 10 台不同设备从同一 IP 频繁切换，更容易触发风控。

---

## 三、Chat 请求头对照

| Header | 文档 | Go 版 | Python 版 |
|--------|------|-------|-----------|
| `Content-Type: application/json` | :white_check_mark: | :white_check_mark: | :white_check_mark: |
| `Authorization: Bearer <JWT>` | :white_check_mark: | :white_check_mark: | :white_check_mark: |
| `X-Mimo-Source: mimocode-cli-free` | :white_check_mark: | :white_check_mark: | :white_check_mark: |
| **`x-session-affinity: ses_<hex>`** | :white_check_mark: | :white_check_mark: | **缺失** |
| **`User-Agent: mimocode/1.0.0`** | - | :white_check_mark: | **缺失** |

---

## 四、Bootstrap 请求头对照

| Header | Go 版 | Python 版 |
|--------|-------|-----------|
| `Content-Type: application/json` | :white_check_mark: | :white_check_mark: |
| `User-Agent: mimocode/1.0.0` | :white_check_mark: | **缺失** |

---

## 五、JWT 刷新策略差异

| | Go 版 | Python 版 |
|---|-------|-----------|
| 刷新方式 | 后台 goroutine 每 30s 检查 | 懒加载，请求时才检查 |
| 提前量 | JWT 到期前 5 分钟刷新 | 到期前 5 分钟（`REFRESH_MARGIN=300`） |
| 启动时 | 立即 bootstrap 填充 cache | lazy，第一个请求触发 |
| 多实例 | 单 JWT 全局共享 | 每个 backend 独立 JWT |

Go 版 `startJwtRefresher` 在后台每 30s 检查一次 JWT 是否快过期，提前刷新。Python 版只在请求到来时检查，如果长时间无请求后突然来一个，第一个请求要多等一次 bootstrap 往返。

---

## 六、HTTP Client 差异

| | Go 版 | Python 版 |
|---|-------|-----------|
| 超时 | `5min + 30s` | `300s` |
| Keep-Alive | TCP 连接复用（自带） | 无（urllib 默认无） |
| Pipeline | 全局单 httpClient | 每个 backend 有独立 opener |

---

## 七、错误处理差异

### 7.1 441 风控错误

| | Go 版 | Python 版 |
|---|-------|-----------|
| 行为 | 上游透传（作为普通错误返回客户端） | **直接 raise HTTPError，记录 ERROR 后停止** |

441 应该透传给客户端（带上游错误消息），当前 Python 版 441 会触发 `raise` 最终变成 502。

### 7.2 401/403 重试

Python 版：401/403 → 刷 JWT → 重试一次（`get_jwt(force=True)`）
Go 版：无重试机制，因为 JWT 由后台自动刷新，不会出现过期情况

Python 版的重试逻辑本身没问题，但如果 JWT 是好的但上游仍返回 403（风控），重试刷 JWT 反而浪费一次 bootstrap 请求。

---

## 八、请求体处理差异

### 8.1 Guard Prompt 注入方式

| | Go 版 | Python 版 |
|---|-------|-----------|
| 方式 | JSON 反序列化 → 检查 → 插入 | 直接插入到消息列表**
| 严格性 | 精确匹配 `antiAbuseMarker` 字符串 | 匹配前80字符前缀 |
| Anthropic 格式 | system 也注入 marker | 无 Anthropic 支持 |

Go 版 `ensureAntiAbuseMarker()` 第 349 行用 `strings.Contains(text, antiAbuseMarker)` 精确匹配，Python 版用 `startswith(MIMO_GUARD_TEXT[:80])`，实际上更宽松但效果一样。

### 8.2 max_tokens 钳制

Python 版强制 `max_tokens ≤ 131072`，Go 版无此限制。文档未提此限制。

---

## 九、路由/端点对照

| 端点 | Go 版 | Python 版 |
|------|-------|-----------|
| `GET /health` | :white_check_mark: | :white_check_mark: |
| `GET /v1/models` | :white_check_mark: | :white_check_mark: |
| `POST /v1/chat/completions` | :white_check_mark: | :white_check_mark: |
| `POST /v1/messages` (Anthropic) | :white_check_mark: | :white_check_mark: |
| 路径容错（无 v1 前缀） | :white_check_mark: | :white_check_mark: |
| CORS | :white_check_mark: | **缺失** |

---

## 十、日志与可观测性

| | Go 版 | Python 版 |
|---|-------|-----------|
| 请求日志 | 无（Go 标准日志） | 仅上游错误时 WARN |
| 441 日志 | 无特殊处理 | **无**（当普通 HTTP 错误） |
| 健康检查详情 | `{"ok": true, "upstream": "..."}` | `{"status": "ok", "backends": N}` |
| JWT 刷新 | 无日志 | INFO 日志 |
| 请求统计 | 无 | 无 |
| 后端计数 | 无 | 无 |

---

## 十一、优先级排序

### P0 — 必改（不改 403 无解）

1. **加 `x-session-affinity` header** — 每个 chat 请求随机生成 `ses_<12hex>`
2. **加 `User-Agent: mimocode/1.0.0`** — bootstrap 和 chat 都带
3. **对齐指纹格式** — 改为 5 字段：`hostname|os|arch|username|uuid`
4. **全局单指纹** — 所有 backend 共用一个指纹（或至少保证格式一致）

### P1 — 应该改

5. **日志加 441 处理** — 透传给客户端，记录 WARN
6. **CORS 支持** — 加 `Access-Control-Allow-*` headers

### P2 — 可选的

7. JWT 后台自动刷新（减少首次请求延迟）
8. 请求统计 + 健康检查增强（backend 请求计数）
9. HTTP Keep-Alive 复用连接
