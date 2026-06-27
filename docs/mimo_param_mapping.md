# MiMo Code Proxy 参数修改说明

本代理将 OpenAI 兼容格式的请求转换为 MiMo Code CLI 的正版请求格式。
以下列出所有被覆盖、注入或删除的参数。

## 请求体修改（chat 请求）

| 参数 | 行为 | 值 | 说明 |
|------|------|-----|------|
| `model` | **强制覆盖** | `mimo-auto` | 用户传任意值均忽略，上游只认此模型 |
| `temperature` | **强制覆盖** | `1.0` | 正版 CLI `transform.ts:515` 固定值 |
| `max_tokens` | 缺失时补默认 | `128000` | 用户传了就用用户的，不钳制 |
| `top_p` | **删除** | — | 正版不发送（`transform.ts:526-532`） |
| `top_k` | **删除** | — | 正版不发送（`transform.ts:535-542`） |
| `provider_options` | **强制覆盖** | `{"mimo": {}}` | 正版固定值（`transform.ts:1126-1165`） |
| `messages` | 缺品牌标识时**注入** | system msg（见下方） | 服务端校验 #9、#10 |

### messages 注入逻辑

扫描所有 `role: system` 的消息，若缺失以下任一字符串则插入一条 system 消息：

- `"MiMo Code Agent, built by Xiaomi MiMo Team"`（品牌标识）
- `"exact model ID is mimo/mimo-auto"`（模型标识）

注入的完整 system 消息：

```
You are MiMo Code Agent, built by Xiaomi MiMo Team.
You are an interactive agent that helps users with software engineering tasks.
You are powered by the model named mimo-auto.
The exact model ID is mimo/mimo-auto.
```

## 请求头注入

以下头**始终**附加到请求中（覆盖用户传递的同名头）：

| Header | 值 | 说明 |
|--------|-----|------|
| `Authorization` | `Bearer {jwt}` | bootstrap 获取的动态 JWT |
| `X-Mimo-Source` | `mimocode-cli-free` | 标识免费客户端来源 |
| `User-Agent` | `mimocode/prod/0.1.3/cli` | 正版 CLI UA |
| `x-session-affinity` | `ses_<12hex><14base62>` | 会话 ID（启动时生成，格式对齐 `id/id.ts`） |
| `Content-Type` | `application/json` | 固定值 |

## 透传参数

以下参数**不做任何修改**，原样传递：

- `stream` — 流式开关
- `tools` — 工具定义列表
- `tool_choice` — 工具选择策略
- `messages` 中的 user/assistant 消息
- 其他未列出的顶层字段

## 请求 URL 映射

| 阶段 | URL |
|------|-----|
| Bootstrap | `POST https://api.xiaomimimo.com/api/free-ai/bootstrap` |
| Chat | `POST https://api.xiaomimimo.com/api/free-ai/openai/chat` |

Chat URL 由 `/v1/chat/completions`（OpenAI 格式）映射而来，请求体中的参数修改在此映射之上执行。
