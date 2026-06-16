# mimo-code-proxy 功能升级设计文档

**日期**：2026-06-16
**状态**：待实现

## 目标

为现有单端口、单指纹的 MiMo 代理增加 4 项功能 + 1 套详细日志系统。
约束不变：Python 3.13、仅标准库、snake_case、4 空格缩进、`log()` 写 stderr、不引外部依赖。

## 范围

要做：
1. 真·流式 SSE 转发
2. `/v1/...` 路径前缀兼容
3. 健康检查含上游探活
4. 指纹动态轮换 + 自动换
5. 详细分级日志系统

不做：
- 请求计数 / 限流（明确排除）

---

## 1. 指纹动态轮换（核心）

### 现状

全局单指纹（一个 UUID）+ 全局单 JWT。指纹固定持久化在 `CLIENT_FILE`。

### 设计决策

| 决策点 | 选择 |
|--------|------|
| 指纹来源 | **动态生成**：坏一个 `uuid.uuid4()` 现造新的，不预置池 |
| 活跃数 | **单活跃**：平时只用 1 个指纹，坏了才替换 |
| 换的触发 | **任何非 2xx 正常返回都换**（429 / 4xx / 5xx / 超时 / 网络错误） |
| 重试上限 | 每客户端请求最多换 `MIMO_MAX_RETRY` 个指纹，默认 3 |

### 状态模型

维护「当前活跃指纹」单一状态，加锁保护：

```
active_fp = {
    "client": "<uuid>",      # 当前指纹
    "jwt": None,             # 该指纹的 JWT
    "jwt_exp": 0,            # JWT 过期时间(ms)
}
```

- 启动时：从 `CLIENT_FILE` 读已有指纹；无则生成并持久化。
- 换指纹：生成新 UUID → 覆盖 `active_fp` → 清空 JWT → 重新 bootstrap → 持久化新指纹到 `CLIENT_FILE`。
- 不保留旧指纹（单活跃，弃用即丢）。

### 请求处理流程

```
收到 chat 请求
  retry = 0
  loop:
    确保 active_fp 有有效 JWT (过期则 bootstrap)
    调用上游 chat
    成功(2xx) -> 转发响应, 结束
    失败(任何非2xx / 异常):
      记录失败原因(上游码)
      retry += 1
      若 retry > MIMO_MAX_RETRY:
        把最后一次上游错误透传给客户端, 结束
      换指纹(生成新 UUID + bootstrap)
      continue
```

关键点：
- **不再区分「JWT 过期」与「指纹失效」**。任何失败统一换指纹（用户明确要求：没拿到正确返回就换）。
- JWT 正常过期（请求前主动检测 exp）仍走同指纹 bootstrap，不计入 retry。只有「发出去的 chat 请求失败」才触发换指纹。
- bootstrap 本身失败也算当前指纹坏 → 换新指纹重试，同样受 `MIMO_MAX_RETRY` 约束。

### 并发

`ThreadingHTTPServer` 多线程。`active_fp` 与换指纹操作全程持锁。
单活跃模型下，多个线程共享同一指纹；某线程换指纹后，其他线程自动用上新指纹。
换指纹在锁内做 double-check：若进锁后发现 `active_fp.client` 已被别的线程换掉，则不重复换，直接复用。

---

## 2. 真·流式 SSE 转发

### 现状

POST 响应固定 `Connection: close`，8192 字节块循环 read/write/flush，靠关连接表示结束。

### 设计

根据请求体 `stream` 字段分流：

- **流式 (`stream=true`)**：
  - 响应头透传上游 `Content-Type`（通常 `text/event-stream`）。
  - 逐块读上游并立即 `write` + `flush`，块大小减小（如 1024）降低首字延迟。
  - 用 `Connection: close` 表示流结束（SSE 标准做法，兼容 OpenAI 客户端）。
  - DEBUG 日志记累计字节数、转发耗时、是否收到 `[DONE]`。

- **非流式 (`stream` 缺省/false)**：
  - 完整缓冲上游响应体。
  - 带 `Content-Length` 返回，`Connection: close`。
  - 支持 keep-alive 语义正确性（当前缺 Content-Length 是 bug）。

实现上两条路径共用上游调用，仅响应写出方式不同。

---

## 3. `/v1/...` 路径前缀兼容

### 现状

`do_GET` 用 `path.endswith("/models")` / `endswith("/health")`；
`do_POST` 用 `"/chat/completions" in self.path`。

OpenAI SDK 默认 base_url 走 `/v1/chat/completions`、`/v1/models`。当前 `in` 判断已能命中 chat，但 models/health 的 `endswith` 对 `/v1/models` 也能命中（结尾匹配）。

### 设计

统一规整路径匹配，显式支持带或不带 `/v1` 前缀：

- `GET /models`、`GET /v1/models` → 模型列表
- `GET /health`、`GET /v1/health` → 健康检查
- `POST /chat/completions`、`POST /v1/chat/completions` → 聊天

做法：取 `self.path` 去查询串、去尾斜杠后，剥掉可选 `/v1` 前缀再精确匹配末段。避免 `endswith` 的宽松匹配带来的歧义。

---

## 4. 健康检查含上游探活

### 现状

`/health` 无条件返回 `{"status": "ok"}`，不验证上游可用性。

### 设计

`/health` 改为反映真实可用性：

- 尝试确保有有效 JWT（必要时 bootstrap，带短超时如 10s）。
- 成功：`200 {"status": "ok", "upstream": "ok", "fp": "<前8位>…"}`
- 失败：`503 {"status": "degraded", "upstream": "down", "error": "<原因>"}`

为避免每次健康检查都打上游，加轻量缓存：距上次成功探活 < 30s 直接返回 ok（用已有 JWT 状态判断即可，不重复 bootstrap）。

---

## 5. 详细分级日志系统

### 目标

开发期 debug 用，要非常详细，能完整串联单个请求的全过程。仍走 `log()` 写 stderr，stdlib only。

### 分级

环境变量 `MIMO_LOG_LEVEL` 控制：`DEBUG` / `INFO` / `WARN` / `ERROR`，默认 `INFO`，开发期设 `DEBUG`。
`log()` 升级为带 level 参数，低于阈值的不输出。

### req_id

每个进入的 HTTP 请求分配 8 位短 id（如 `uuid4().hex[:8]`），贯穿该请求所有日志行，格式：

```
[HH:MM:SS] [DEBUG] [req=a1b2c3d4] <消息>
```

### DEBUG 级记录内容

| 时机 | 记录 |
|------|------|
| 收到请求 | 方法、路径、req_id |
| 解析请求体 | model、stream、max_tokens、**完整 messages 内容**（用户明确要求记正文） |
| 指纹状态 | 当前指纹（前 8 位脱敏 `a1b2c3d4…`）、JWT 是否有效、exp 剩余秒 |
| bootstrap | 请求指纹、响应码、新 JWT exp |
| 调用上游 chat | URL、用哪个指纹、第几次 retry |
| 上游响应 | 状态码、Content-Type、是否流式 |
| 换指纹 | 触发原因（哪个上游码 / 异常）、旧指纹→新指纹（均脱敏） |
| 流式转发 | 累计字节、耗时、是否收到 `[DONE]` |
| 请求结束 | 总耗时、最终状态码 |

### 脱敏规则

- 指纹 client：日志只显示前 8 位 + `…`。
- JWT：永不打印完整 token，只记 exp 时间和「有效/过期」。
- messages 正文：按用户要求**完整记录**（开发期，知晓隐私权衡）。

⚠️ 注意：DEBUG 级会把用户对话正文写入 stderr 日志，生产环境应调回 `INFO` 避免隐私泄露。文档中需在 README 标注此风险。

---

## 配置变量汇总（新增）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MIMO_MAX_RETRY` | 3 | 单请求最多换几个指纹 |
| `MIMO_LOG_LEVEL` | INFO | 日志级别 DEBUG/INFO/WARN/ERROR |

保留现有：`MIMO_HOST`、`MIMO_PORT`、`MIMO_KEY`、`MIMO_UPSTREAM`、`MIMO_CLIENT_FILE`、`MIMO_CLIENT_ID`。

---

## 测试要点

沿用 `unittest`（stdlib）。新增/调整：

- 指纹轮换：模拟上游连续失败，验证换指纹次数 ≤ MIMO_MAX_RETRY，超限透传最后错误。
- 换指纹 double-check：并发下不重复换。
- 流式 vs 非流式：stream=true 走逐块 flush；false 带 Content-Length。
- 路径兼容：`/models`、`/v1/models`、`/chat/completions`、`/v1/chat/completions` 全命中。
- 健康检查：JWT 正常 → 200 ok；bootstrap 失败 → 503 degraded。
- 日志：DEBUG 级输出含 req_id；指纹脱敏；级别过滤生效。

---

## 风险与权衡

1. **DEBUG 记 messages 正文** → 隐私泄露风险，仅限开发期，README 警示。
2. **任何失败都换指纹** → 上游短暂抖动（5xx）会误杀当前指纹，但用户明确要此行为，简单优先。
3. **动态生成指纹是否始终可用** → 依赖上次会话验证（随机指纹能过 bootstrap）。若某天上游收紧，需回退到预置池策略。
