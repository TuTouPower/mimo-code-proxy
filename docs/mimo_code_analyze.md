# MiMo Code `mimo/mimo-auto` 免费模型全链路网络请求分析

> 逆向分析日期: 2026-06-27
> 分析版本: MiMo Code v0.1.3 (prod channel)

## 概述

MiMo Code 的 `mimo/mimo-auto` 免费模型通过内置扩展 `mimo-free` 注册，使用 `@ai-sdk/openai-compatible` SDK。有两个网络请求：

1. **Bootstrap 请求** — 获取临时 JWT token
2. **LLM Chat 请求** — 实际的模型推理调用

---

## 请求一: Bootstrap (获取 JWT)

### 触发时机

首次 LLM 请求前，JWT 不存在或已过期时自动触发。

### 请求详情

| 项目 | 值 |
|------|-----|
| URL | `https://api.xiaomimimo.com/api/free-ai/bootstrap` |
| 方法 | `POST` |
| Content-Type | `application/json` |

### 请求体

```json
{
  "client": "<device_fingerprint>"
}
```

### device_fingerprint 生成逻辑

代码位置: 二进制扩展 `mimo-free` (源码 `packages/opencode/src/ext/mimo-free.ts`，编译后嵌入二进制)

```
fingerprint = SHA256(hostname + "|" + platform + "|" + arch + "|" + cpu_model + "|" + username)
```

字段来源:
- `hostname`: `os.hostname()`
- `platform`: 系统平台 (`"linux"` / `"darwin"` / `"win32"`)
- `arch`: CPU 架构 (`"x64"` / `"arm64"`)
- `cpu_model`: `os.cpus()[0]?.model`
- `username`: `os.userInfo().username`

⚠️ **关键校验点**: fingerprint 被服务端用于唯一标识设备，必须使用真实系统信息。

fingerprint 计算后缓存到 `{MIMOCODE_DATA_DIR}/mimo-free-client` 文件，后续启动复用。

### 响应

```json
{
  "jwt": "<jwt_token_string>"
}
```

### JWT 解析

- JWT payload 含 `exp` 字段 (Unix 秒)
- 过期前 5 分钟 (300s) 自动续期
- 401/403 时立即强制刷新

---

## 请求二: LLM Chat (模型推理)

### 触发时机

用户在 TUI 中输入消息并发送时触发。

### 请求详情

| 项目 | 值 |
|------|-----|
| URL | `https://api.xiaomimimo.com/api/free-ai/openai/chat` |
| 方法 | `POST` |
| Content-Type | `application/json` |

> URL 改写逻辑: AI SDK 会 POST 到 `{baseURL}/chat/completions`，自定义 fetch 函数将其改写为 `{baseURL}/chat`

### 请求头

| Header | 值 | 代码来源 |
|--------|-----|----------|
| `Authorization` | `Bearer {jwt}` | mimo-free 扩展自定义 fetch |
| `X-Mimo-Source` | `mimocode-cli-free` | mimo-free 扩展自定义 fetch |
| `Content-Type` | `application/json` | AI SDK 默认 |
| `User-Agent` | `mimocode/{channel}/{version}/cli` | `session/llm.ts:607` |
| `x-session-affinity` | `{sessionID}` | `session/llm.ts:603` |
| `x-parent-session-id` | `{parentSessionID}` (可选) | `session/llm.ts:604` |

> 注意: `packages/opencode/src/plugin/mimo.ts:198` 的 `chat.headers` hook 只对 `xiaomi` provider 生效，不影响 `mimo` provider。

### 请求体参数

#### model 信息

| 字段 | 值 | 代码来源 |
|------|-----|----------|
| model | `mimo-auto` | `provider/provider.ts:1719-1721` (默认模型选择) |
| providerID | `mimo` | mimo-free 扩展注册 |

model 定义 (mimo-free 扩展注册):
- `name`: "MiMo Auto"
- `context`: 1,000,000 tokens
- `max output`: 128,000 tokens
- `modalities`: text, image (输入)
- `reasoning`: 支持
- `tool_call`: 支持
- `cost`: input=0, output=0 (免费)

#### temperature / topP / topK

代码位置: `packages/opencode/src/provider/transform.ts`

| 参数 | 值 | 行号 | 逻辑 |
|------|-----|------|------|
| `temperature` | `1.0` | `transform.ts:515` | `id.includes("mimo")` → 1.0 |
| `topP` | `undefined` | `transform.ts:526-532` | mimo 不在条件列表中 |
| `topK` | `undefined` | `transform.ts:535-542` | mimo 不在条件列表中 |

#### maxOutputTokens

代码位置: `packages/opencode/src/provider/transform.ts:1167-1172`

```typescript
if (model.providerID === "mimo" || model.providerID === "xiaomi" || ...)
  return MIMO_OUTPUT_TOKEN_MAX  // 128,000
return Math.min(model.limit.output, OUTPUT_TOKEN_MAX) || OUTPUT_TOKEN_MAX
```

**值**: `128000`

#### providerOptions

代码位置: `packages/opencode/src/provider/transform.ts:948-1083`

mimo provider 的 `options()` 函数:
- `npm` 是 `@ai-sdk/openai-compatible`，进入 `providerOptions()` (`transform.ts:1126`):
  - key 为 `model.providerID` ("mimo")

**providerOptions 值**: `{ "mimo": {} }` (空，因为 mimo 不在任何特殊条件中)

#### 实际请求体示例

```json
{
  "model": "mimo-auto",
  "messages": [
    {
      "role": "system",
      "content": "<system_prompt>"
    },
    {
      "role": "user",
      "content": "<user_message>"
    }
  ],
  "temperature": 1.0,
  "max_tokens": 128000,
  "stream": true,
  "tools": [...]
}
```

> 实际的 messages 数组由 `message-v2.ts:toModelMessages()` 构建，system prompt 由 `system.ts:SystemPrompt.provider()` 提供。

### API 认证流程

代码位置: mimo-free 扩展自定义 fetch 函数

```
1. 检查 JWT 是否有效 (exp - now > 300s)
2. 若无效: 异步获取新 JWT (请求一)
3. 发起 LLM 请求
4. 若 401/403: 清除缓存 JWT，重新获取，重试
```

---

## 完整调用链

### 启动阶段

```
mimo 命令行入口
  → index.ts:71 yargs CLI
  → index.ts:92-168 middleware (日志初始化, 数据库迁移)
  → plugin/index.ts:128-142 INTERNAL_PLUGINS 加载
    → mimo-free 扩展加载 (plugin/index.ts:273-315 ext/ 目录)
      → MimoFreeAuthPlugin.config() 注册 "mimo" provider
        → provider.mimo = { name: "MiMo Auto (free)", api: "https://...", models: { "mimo-auto": {...} } }
```

### 发消息阶段

```
用户输入 (TUI)
  → session/session.ts (消息处理)
  → session/llm.ts:LLM.stream()
    → 行 298-645 LLM.run()
    → 行 312-320 provider.getLanguage(model) + config + auth
    → 行 325-334 buildSystemArray() (system prompt)
    → 行 340-397 参数构建:
      - 行 341: ProviderTransform.smallOptions()
      - 行 342: ProviderTransform.options()
      - 行 347-352: mergeDeep 合并 agent.options, variant, model.options
      - 行 368-390: params (temperature, topP, topK, maxOutputTokens)
      - 行 392-404: headers (chat.headers hook)
    → 行 406 resolveTools() (工具解析)
    → 行 554-644 streamText() (AI SDK 调用)
      → 行 596: providerOptions = ProviderTransform.providerOptions()
      → 行 600: maxOutputTokens
      → 行 602-608: headers
      → 行 619-634: wrapLanguageModel → transformParams middleware
        → 行 628: ProviderTransform.message(messages, model, options)
    → AI SDK @ai-sdk/openai-compatible
      → POST {baseURL}/chat/completions
      → 自定义 fetch 拦截 (mimo-free):
        → 改写 URL: /chat/completions → /chat
        → 添加 Authorization: Bearer {jwt}
        → 添加 X-Mimo-Source: mimocode-cli-free
        → JWT 过期自动刷新
```

---

## 参数校验合规分析

服务端校验的参数 (需与正版客户端一致):

| 校验项 | 关键字段 | 说明 |
|--------|----------|------|
| **设备指纹** | `client` (bootstrap body) | SHA256(hostname\|os\|arch\|cpu\|user) |
| **JWT 认证** | `Authorization: Bearer {jwt}` | 必须通过 bootstrap 获取 |
| **来源标识** | `X-Mimo-Source: mimocode-cli-free` | 标识为免费客户端 |
| **User-Agent** | `mimocode/{channel}/{version}/cli` | 生产环境: `mimocode/prod/0.1.3/cli` |
| **API 路径** | `/api/free-ai/openai/chat` | 必须用自定义 fetch 改写 |
| **模型参数** | `model: mimo-auto`, `temperature: 1.0`, `max_tokens: 128000` | 与正版一致 |

---

## 关键代码文件索引

| 文件 | 关键内容 |
|------|----------|
| `packages/opencode/src/index.ts:71-262` | CLI 入口, 中间件初始化 |
| `packages/opencode/src/plugin/index.ts:128-315` | 插件/扩展加载, mimo-free 扩展注册 |
| `packages/opencode/src/plugin/mimo.ts:85-201` | MimoAuthPlugin (xiaomi provider, 不影响 mimo) |
| `packages/opencode/src/provider/provider.ts:1086-1735` | Provider 状态初始化, custom loaders, defaultModel |
| `packages/opencode/src/provider/transform.ts:507-1172` | temperature/topP/topK/maxTokens/options 计算 |
| `packages/opencode/src/session/llm.ts:298-645` | LLM.stream() 请求构建 |
| `packages/opencode/src/session/message-v2.ts:615-872` | toModelMessages() 消息转换 |
| `packages/opencode/src/session/system.ts` | SystemPrompt.provider() 系统提示词 |
| `packages/opencode/src/installation/index.ts:58` | USER_AGENT 定义 |
| `packages/opencode/src/provider/models.ts:110-180` | models.dev 数据加载与缓存 |

### mimo-free 扩展 (嵌入二进制)

| 逻辑 | 说明 |
|------|------|
| 设备指纹生成 | SHA256(hostname\|platform\|arch\|cpu\|username) |
| Bootstrap | `POST https://api.xiaomimimo.com/api/free-ai/bootstrap` → JWT |
| LLM Chat | `POST https://api.xiaomimimo.com/api/free-ai/openai/chat` (改写自 /chat/completions) |
| Provider 注册 | `api: L`, `npm: @ai-sdk/openai-compatible`, `apiKey: "anonymous"` |
| 自定义 fetch | 添加 Auth headers + URL 改写 + JWT 自动刷新 |
| baseURL 环境变量 | `MIMO_FREE_BASE_URL` 可覆盖默认 URL |

---

## 伪装请求的最小实现

```python
import hashlib
import time
import jwt
import requests
import os
import platform

BASE_URL = os.environ.get("MIMO_FREE_BASE_URL", "https://api.xiaomimimo.com")
BOOTSTRAP_URL = f"{BASE_URL}/api/free-ai/bootstrap"
CHAT_URL = f"{BASE_URL}/api/free-ai/openai/chat"
UA = "mimocode/prod/0.1.3/cli"

def get_fingerprint():
    hostname = platform.node()
    os_name = platform.system().lower()
    arch = platform.machine().lower()
    cpu = os.popen("cat /proc/cpuinfo | grep 'model name' | head -1").read()
    cpu = cpu.split(":")[-1].strip() if cpu else "unknown"
    username = os.getlogin()
    raw = f"{hostname}|{os_name}|{arch}|{cpu}|{username}"
    return hashlib.sha256(raw.encode()).hexdigest()

def get_jwt():
    resp = requests.post(BOOTSTRAP_URL,
        json={"client": get_fingerprint()},
        headers={"Content-Type": "application/json", "User-Agent": UA})
    return resp.json()["jwt"]

def chat(messages, jwt_token):
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "X-Mimo-Source": "mimocode-cli-free",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    body = {
        "model": "mimo-auto",
        "messages": messages,
        "temperature": 1.0,
        "max_tokens": 128000,
        "stream": True,
    }
    return requests.post(CHAT_URL, json=body, headers=headers, stream=True)
```
