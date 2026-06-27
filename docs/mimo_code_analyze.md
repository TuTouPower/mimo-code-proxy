# MiMo Code `mimo/mimo-auto` 免费模型全链路网络请求分析

> 逆向分析日期: 2026-06-27
> 分析版本: MiMo Code v0.1.3 (prod channel)

## 概述

MiMo Code 的 `mimo/mimo-auto` 免费模型通过内置扩展 `mimo-free` 注册，使用 `@ai-sdk/openai-compatible` SDK 封装的 OpenAI 兼容 API。共 2 个网络请求。

---

## 请求一: Bootstrap (获取 JWT)

### URL & Method

```
POST https://api.xiaomimimo.com/api/free-ai/bootstrap
```

### 请求头

| Header | 值 | 代码来源 |
|--------|-----|----------|
| `Content-Type` | `application/json` | 扩展代码 (`fetch` 参数) |
| `User-Agent` | `mimocode/{channel}/{version}/cli` | todo (实际由 fetch 默认, 未显式设置) |

### 请求体

```json
{"client": "<device_fingerprint>"}
```

#### `client` 参数生成

代码位置: mimo-free 扩展

```
fingerprint = SHA256(hostname + "|" + os + "|" + arch + "|" + cpu_model + "|" + username)
```

| 组成 | 来源 | 示例 |
|------|------|------|
| hostname | `os.hostname()` | `my-machine` |
| os | `process.platform` | `linux` |
| arch | `os.arch()` | `x64` |
| cpu_model | `os.cpus()[0]?.model` | `Intel(R) Core(TM) i7-...` |
| username | `os.userInfo().username` | `karon` |

fingerprint 首次计算后缓存到 `{MIMOCODE_DATA_DIR}/mimo-free-client` 文件，后续复用。

### 响应

```json
{"jwt": "<jwt_token_string>"}
```

JWT payload 含 `exp` 字段。过期前 300 秒自动续期；收到 401/403 时立即强制刷新。

---

## 请求二: LLM Chat

### URL & Method

```
POST https://api.xiaomimimo.com/api/free-ai/openai/chat
```

> AI SDK 原本 POST 到 `{baseURL}/chat/completions`，mimo-free 自定义 fetch 将 URL 中 `/chat/completions` 改写为 `/chat`。

### 请求头

| Header | 值 | 代码来源 |
|--------|-----|----------|
| `Authorization` | `Bearer {jwt}` | mimo-free 自定义 fetch |
| `X-Mimo-Source` | `mimocode-cli-free` | mimo-free 自定义 fetch |
| `Content-Type` | `application/json` | AI SDK |
| `User-Agent` | `mimocode/{channel}/{version}/cli` | `session/llm.ts:607` |
| `x-session-affinity` | `{sessionID}` | `session/llm.ts:603` |
| `x-parent-session-id` | `{parentSessionID}` (仅子会话) | `session/llm.ts:604` |

User-Agent 格式: `mimocode/{InstallationChannel}/{InstallationVersion}/{Flag.MIMOCODE_CLIENT}`
- `InstallationChannel`: 来自编译时常量 `MIMOCODE_CHANNEL` (prod= `"prod"`)
- `InstallationVersion`: 来自编译时常量 `MIMOCODE_VERSION` (当前 `"0.1.3"`)
- `Flag.MIMOCODE_CLIENT`: 环境变量 `MIMOCODE_CLIENT` 或默认 `"cli"`

生产环境: `mimocode/prod/0.1.3/cli`

> 注意: `packages/opencode/src/plugin/mimo.ts:197-200` 的 `chat.headers` hook 只对 `xiaomi` provider 生效，不影响 `mimo` provider。

### 请求体完整参数

#### 顶层字段

| 参数 | 值 | 代码来源 |
|------|-----|----------|
| `model` | `"mimo-auto"` | `provider/provider.ts:1719-1721` + mimo-free 扩展注册 |
| `temperature` | `1.0` | `provider/transform.ts:515` |
| `top_p` | 不发送 | `provider/transform.ts:526-532` (mimo 不在条件列表, 返回 `undefined`) |
| `top_k` | 不发送 | `provider/transform.ts:535-542` (mimo 不在条件列表, 返回 `undefined`) |
| `max_tokens` | `128000` | `provider/transform.ts:1167-1172` |
| `stream` | `true` | AI SDK `streamText()` 默认 |
| `messages` | 见下方 | `session/llm.ts:362-370` + `message-v2.ts:toModelMessages()` |
| `tools` | 见下方 | `session/prompt.ts:resolveTools()` |
| `tool_choice` | `"auto"` | `session/llm.ts:599` (默认) |
| `provider_options` | `{"mimo": {}}` | `provider/transform.ts:1126-1165` |

#### `messages` 数组 (完整)

messages 由 `session/llm.ts:362-370` 构建，格式为 system 消息数组 + 历史消息:

```typescript
[...system.map(x => ({role: "system", content: x})), ...input.messages]
```

`system` 数组由 `session/llm.ts:buildSystemArray()` + `session/llm-request-prefix.ts:buildLLMRequestPrefix()` 构建。对于 mimo/mimo-auto，system 消息按顺序包含：

##### System Message 1: 基础 System Prompt + Agent Prompt

代码: `session/system.ts:20-35` + `session/llm.ts:234-254`

对于 `mimo-auto` (model.api.id = `"mimo-auto"`), `SystemPrompt.provider()` 返回 `PROMPT_DEFAULT` (`session/prompt/default.txt`):

```
You are MiMoCode, an interactive CLI tool that helps users with software
engineering tasks. Use the instructions below and the tools available to you
to assist the user.

IMPORTANT: You must NEVER generate or guess URLs for the user unless you are
confident that the URLs are for helping the user with programming...

# Tone and style
You should be concise, direct, and to the point...

# Text output
Assume users can't see most tool calls — only your text output...

# Proactiveness
You are allowed to be proactive, but only when the user asks you to do something...

# Following conventions
When making changes to files, first understand the file's code conventions...

# Code style
- IMPORTANT: DO NOT ADD ***ANY*** COMMENTS unless asked...

# Doing tasks
The user will primarily request you perform software engineering tasks...

# Executing actions with care
Carefully consider the reversibility and blast radius of actions...

# Git safety
- NEVER update the git config...

# Avoid unnecessary sleep commands...

# Tool usage policy
- When doing file search, prefer to use the actor tool...

# Code References
When referencing specific functions or pieces of code include the pattern
`file_path:line_number`...
```

> 完整内容见 `packages/opencode/src/session/prompt/default.txt`

如果 agent 配置了 `agent.prompt`，它会被拼接到基础 prompt 后面 (`llm.ts:245-253`)。

##### System Message 2: 环境信息

代码: `session/system.ts:50-68` + `session/llm-request-prefix.ts:270` (`sys.environment(model, captureSession.time.created)`)

```
You are MiMo Code Agent, built by Xiaomi MiMo Team. You are an interactive
agent that helps users with software engineering tasks. Use the instructions
below and the tools available to you to assist the user.
You are powered by the model named mimo-auto. The exact model ID is mimo/mimo-auto
Here is some useful information about the environment you are running in:
<env>
  Working directory: {Instance.directory}
  Workspace root folder: {Instance.worktree}
  Is directory a git repo: {yes/no}
  Platform: {process.platform}
  Today's date: {new Date(session.time.created).toDateString()}
</env>
IMPORTANT: Your response must ALWAYS strictly follow the same major language as the user.
```

##### System Message 3: Memory 系统指令

代码: `session/llm.ts:99-179` + `session/llm.ts:266-280` (`buildMemoryInstructions()`)

```
# Memory system

You have a persistent file-based memory system. Four file types:

- Project memory at `{memoryRoot}/projects/{projectID}/MEMORY.md`
- Session checkpoint at `{memoryRoot}/sessions/{sessionID}/checkpoint.md`
- Per-task progress at `{memoryRoot}/sessions/{sessionID}/tasks/<id>/progress.md`
- Global memory at `{memoryRoot}/global/MEMORY.md`

... (完整内容见 session/llm.ts:104-178)
```

##### System Message 4: Skills (可选)

代码: `session/system.ts:73-85` + `session/llm-request-prefix.ts:268-269` (`sys.skills(ag)`)

如果 skills 可用:
```
Skills provide specialized instructions and workflows for specific tasks.
Use the skill tool to load a skill when a task matches its description.
<available skills list>
```

##### System Message 5: Instructions

代码: `session/llm-request-prefix.ts:271` (`instruction.system()`)

从项目配置和 CLAUDE.md 等文件加载的自定义指令。

##### 历史消息

代码: `message-v2.ts:615-872` (`toModelMessages()`)

将数据库中存储的对话历史转换为 AI SDK 的 `ModelMessage[]` 格式。包括 user/assistant 交替的消息，每个 assistant 消息中嵌入 tool-call 和 tool-result。

##### User Message (当前)

最后一条是当前用户输入的消息。

#### `tools` 参数

代码: `session/prompt.ts:625-955` (`resolveTools()`)

包含所有注册的工具定义 (bash, read, write, edit, grep, glob, task, actor, lsp, websearch, webfetch 等)，每个工具以 JSON Schema 描述其参数。

格式:
```json
[
  {
    "type": "function",
    "function": {
      "name": "bash",
      "description": "Execute a bash command...",
      "parameters": { "type": "object", "properties": {...}, "required": [...] }
    }
  },
  ...
]
```

#### 完整请求体示例 (首次对话)

```json
{
  "model": "mimo-auto",
  "messages": [
    {
      "role": "system",
      "content": "You are MiMoCode, an interactive CLI tool that helps users with software engineering tasks...\n\n# Tone and style\n..."
    },
    {
      "role": "system",
      "content": "You are MiMo Code Agent, built by Xiaomi MiMo Team...\n<env>\n  Working directory: /home/user/project\n  Workspace root folder: /home/user/project\n  Is directory a git repo: yes\n  Platform: linux\n  Today's date: Fri Jun 27 2026\n</env>\nIMPORTANT: Your response must ALWAYS strictly follow the same major language as the user."
    },
    {
      "role": "system",
      "content": "# Memory system\n\nYou have a persistent file-based memory system..."
    },
    {
      "role": "user",
      "content": "帮我写一个 hello world"
    }
  ],
  "temperature": 1.0,
  "max_tokens": 128000,
  "stream": true,
  "tools": [
    {"type": "function", "function": {"name": "bash", "description": "...", "parameters": {...}}},
    {"type": "function", "function": {"name": "read", "description": "...", "parameters": {...}}},
    ...
  ]
}
```

---

## 完整调用链

### 启动阶段

```
mimo CLI (index.ts:71 yargs)
  → index.ts:92-168 middleware (日志, 数据库迁移)
  → plugin/index.ts:128-142 INTERNAL_PLUGINS 加载
    → mimo-free 扩展 (plugin/index.ts:273-315 ext/ 目录扫描)
      → MimoFreeAuthPlugin.config()
        → 注册 provider "mimo":
          name: "MiMo Auto (free)"
          npm: "@ai-sdk/openai-compatible"
          api: "https://api.xiaomimimo.com/api/free-ai/openai"
          options: { apiKey: "anonymous", fetch: customFetch }
          models: { "mimo-auto": { name: "MiMo Auto", reasoning: true, tool_call: true, ... } }
```

### 发消息阶段

```
用户输入 (TUI submit)
  → session/prompt.ts:1770 prompt()
    → session/prompt.ts:1894 runLoop()
      → session/prompt.ts:1930-1960 loop body
        → 行 1995 buildLLMRequestPrefix()
          → llm-request-prefix.ts:30-82
            → 行 48 toModelMessages() (历史消息转换)
            → 行 57 llm.buildSystemArray() (system prompt 数组)
              → SystemPrompt.provider(model) → PROMPT_DEFAULT
              → sys.environment(model, now) → 环境描述
              → buildMemoryInstructions() → 内存系统指令
              → sys.skills() → 可用技能列表
              → instruction.system() → 自定义指令
            → 行 67-79 resolveTools() → 工具定义
        → 行 1998 resolveTools() (最终工具表)
        → 行 2009 llm.stream()
          → session/llm.ts:298 LLM.run()
            → 行 312 provider.getLanguage(model) → 解析 SDK
            → 行 327 buildSystemArray() → system prompt 数组
            → 行 341-352 参数构建:
              ProviderTransform.smallOptions() → {}
              ProviderTransform.options() → {}
              mergeDeep agent.options, variant, model.options → {}
            → 行 372-390 chat.params hook + 参数组装:
              temperature = 1.0 (transform.ts:515)
              topP = undefined (transform.ts:526-532)
              topK = undefined (transform.ts:535-542)
              maxOutputTokens = 128000 (transform.ts:1168-1169)
              options = { "mimo": {} }
            → 行 392-404 chat.headers hook + headers 组装
            → 行 406 resolveTools() → 最终 tools
            → 行 554-644 streamText():
              messages = [system...].concat(input.messages)  (行 362-370)
              headers = { x-session-affinity, User-Agent, ... }  (行 602-608)
              providerOptions = ProviderTransform.providerOptions() → { mimo: {} }
              wrapLanguageModel middleware (行 619-634):
                transformParams → ProviderTransform.message(messages, model, options)
            → AI SDK @ai-sdk/openai-compatible:
              POST {baseURL}/chat/completions
                ↓
              mimo-free 自定义 fetch 拦截:
                1. URL 改写: /chat/completions → /chat
                2. JWT 有效性检查 (exp - now > 300s)
                3. 无效则 POST bootstrap 获取新 JWT
                4. 添加 Authorization: Bearer {jwt}
                5. 添加 X-Mimo-Source: mimocode-cli-free
                6. 发起请求
                7. 401/403: 清除 JWT, 重新获取, 重试一次
```

---

## 参数校验合规清单

服务端校验的参数:

| # | 校验项 | 字段/位置 | 值/生成逻辑 |
|---|--------|-----------|-------------|
| 1 | 设备指纹 | bootstrap body `client` | SHA256(hostname\|os\|arch\|cpu\|username) |
| 2 | JWT | chat header `Authorization` | `Bearer {bootstrap_response.jwt}` |
| 3 | 来源标识 | chat header `X-Mimo-Source` | `mimocode-cli-free` |
| 4 | UA | chat header `User-Agent` | `mimocode/prod/0.1.3/cli` |
| 5 | API 路径 | chat URL | `/api/free-ai/openai/chat` (非 `/chat/completions`) |
| 6 | 模型名 | body `model` | `mimo-auto` |
| 7 | temperature | body `temperature` | `1.0` |
| 8 | max_tokens | body `max_tokens` | `128000` |
| 9 | model identifier | system message 2 | `"The exact model ID is mimo/mimo-auto"` + `"You are powered by the model named mimo-auto"` |
| 10 | 客户端品牌 | system message 2 | `"You are MiMo Code Agent, built by Xiaomi MiMo Team"` |

---

## 关键代码文件索引

| 文件 | 行号 | 内容 |
|------|------|------|
| `packages/opencode/src/index.ts` | 71-262 | CLI 入口, middleware |
| `packages/opencode/src/plugin/index.ts` | 128-315 | 插件/扩展加载 |
| `packages/opencode/src/plugin/mimo.ts` | 85-201 | MimoAuthPlugin (xiaomi, 不影响 mimo) |
| `packages/opencode/src/provider/provider.ts` | 1086-1735 | Provider 初始化, defaultModel |
| `packages/opencode/src/provider/transform.ts` | 507-515 | `temperature()` — mimo → 1.0 |
| `packages/opencode/src/provider/transform.ts` | 526-532 | `topP()` — mimo → undefined |
| `packages/opencode/src/provider/transform.ts` | 535-542 | `topK()` — mimo → undefined |
| `packages/opencode/src/provider/transform.ts` | 948-1083 | `options()` — mimo → {} |
| `packages/opencode/src/provider/transform.ts` | 1085-1118 | `smallOptions()` — mimo → {} |
| `packages/opencode/src/provider/transform.ts` | 1126-1165 | `providerOptions()` — → `{mimo: {}}` |
| `packages/opencode/src/provider/transform.ts` | 1167-1172 | `maxOutputTokens()` — mimo → 128000 |
| `packages/opencode/src/provider/transform.ts` | 450-486 | `message()` — messages 转换 |
| `packages/opencode/src/provider/transform.ts` | 495-505 | `tools()` — 工具定义转换 |
| `packages/opencode/src/session/llm.ts` | 298-645 | `LLM.run()` — 核心请求构建 |
| `packages/opencode/src/session/llm.ts` | 234-296 | `buildSystemArray()` |
| `packages/opencode/src/session/llm.ts` | 99-179 | `buildMemoryInstructions()` |
| `packages/opencode/src/session/llm-request-prefix.ts` | 30-82 | `buildLLMRequestPrefix()` |
| `packages/opencode/src/session/system.ts` | 20-35 | `SystemPrompt.provider()` — mimo → PROMPT_DEFAULT |
| `packages/opencode/src/session/system.ts` | 50-68 | `environment()` — 环境描述 |
| `packages/opencode/src/session/system.ts` | 73-85 | `skills()` — 技能列表 |
| `packages/opencode/src/session/message-v2.ts` | 615-872 | `toModelMessages()` — 历史消息转换 |
| `packages/opencode/src/session/prompt.ts` | 625-955 | `resolveTools()` — 工具注册 |
| `packages/opencode/src/session/prompt.ts` | 1814-1910+ | `runLoop()` — 主循环 |
| `packages/opencode/src/session/prompt/default.txt` | — | 默认 system prompt 文本 |
| `packages/opencode/src/installation/index.ts` | 58 | `USER_AGENT` 定义 |
| `packages/opencode/src/installation/version.ts` | 1-6 | `InstallationVersion` / `InstallationChannel` |

### mimo-free 扩展 (源码 `packages/opencode/src/ext/mimo-free.ts`，编译后嵌入二进制)

| 逻辑 | 说明 |
|------|------|
| baseURL | `https://api.xiaomimimo.com` (可被 `MIMO_FREE_BASE_URL` 覆盖) |
| Bootstrap URL | `{baseURL}/api/free-ai/bootstrap` |
| Chat URL | `{baseURL}/api/free-ai/openai/chat` |
| 设备指纹 | SHA256(hostname\|platform\|arch\|cpu\|username)，缓存到 `{data}/mimo-free-client` |
| Provider 注册 | `config()` hook → `provider.mimo = { api: chatBaseUrl, models: { "mimo-auto": {...} } }` |
| 自定义 fetch | URL 改写 + JWT 管理 + 认证头注入 |
| JWT 刷新 | 过期前 300s 自动续期; 401/403 强制刷新; 并发请求共享同一刷新 Promise |

---

## 伪装请求参考实现

```python
import hashlib
import os
import platform
import requests

BASE = os.environ.get("MIMO_FREE_BASE_URL", "https://api.xiaomimimo.com")
BOOTSTRAP_URL = f"{BASE}/api/free-ai/bootstrap"
CHAT_URL = f"{BASE}/api/free-ai/openai/chat"
UA = "mimocode/prod/0.1.3/cli"

def fingerprint():
    raw = "|".join([
        platform.node(),
        platform.system().lower(),
        platform.machine().lower(),
        os.popen("cat /proc/cpuinfo | grep 'model name' | head -1").read().split(":")[-1].strip() or "unknown",
        os.getlogin(),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()

def get_jwt():
    r = requests.post(BOOTSTRAP_URL,
        json={"client": fingerprint()},
        headers={"Content-Type": "application/json", "User-Agent": UA})
    return r.json()["jwt"]

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
