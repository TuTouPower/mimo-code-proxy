# OpenAI 格式参数转换说明

将 OpenAI Chat Completions API 请求转为 MiMo Code `mimo/mimo-auto` 通道请求时，以下参数会被修改。

## 请求体参数

| 参数 | 行为 | 值 | 说明 |
|------|------|-----|------|
| `model` | **强制覆盖** | `mimo-auto` | — |
| `temperature` | **强制覆盖** | `1.0` | — |
| `max_tokens` | **强制覆盖** | `128000` | — |
| `stream` | **强制覆盖** | `true` | 正版 CLI 始终发 `stream: true` |
| `stream_options` | **强制覆盖** | `{"include_usage": true}` | `provider.ts:1441-1443` 固定值 |
| `top_p` | **删除** | — | 正版不发送此参数 |
| `top_k` | **删除** | — | 正版不发送此参数 |
| `messages` | **前面插入** 2 条 system msg | 见下方 | — |
| 其余字段 | **删除** | — | 白名单过滤，非白名单字段丢弃 |

### messages 前置插入

在 messages 数组最前面插入两条 system 消息：

1. `"You are MiMoCode, an interactive CLI tool that helps users with software engineering tasks."`
2. `"You are MiMo Code Agent, built by Xiaomi MiMo Team. You are an interactive agent that helps users with software engineering tasks."`
