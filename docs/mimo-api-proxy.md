# MiMo Code 完整请求链路分析

## 1. 总体架构

```
用户输入 (TUI/CLI)
  │
  ▼
SessionPrompt.prompt()               ← 创建用户消息
  │
  ▼
SessionPrompt.runLoop()              ← 对话循环
  │
  ├─► SystemPrompt.environment()     ← 环境信息注入
  ├─► SystemPrompt.skills()          ← 技能列表注入
  ├─► LLM.buildSystemArray()         ← 系统提示词拼接
  ├─► ProviderTransform.message()    ← 消息预处理（通过 AI SDK middleware）
  ├─► ProviderTransform.tools()      ← Tool 缓存标记
  ├─► ProviderTransform.options()    ← 模型参数构建
  │
  ▼
LLM.stream()
  │
  ├─► provider.getLanguage(model)    ← 解析 SDK → 含 baseURL/apiKey/fetch/headers
  ├─► plugin "chat.params"           ← 修改 temperature/topP/maxTokens 等
  ├─► plugin "chat.headers"          ← 注入额外 HTTP 头
  │
  ▼
AI SDK streamText()
  │
  ├─► OpenAICompatibleChatLanguageModel.doStream()
  │     ├─► convertToOpenAICompatibleChatMessages()  ← 消息格式转换
  │     ├─► prepareTools()                            ← Tool 格式转换
  │     └─► postJsonToApi()                           ← 实际 HTTP POST
  │
  ▼
provider.ts 自定义 fetch 包装
  │
  ├─► 合并 AbortSignal（请求信号 + chunk超时 + 全局超时）
  ├─► 调用原生 fetch（或 customFetch）
  └─► wrapSSE() — 监控每个 SSE chunk 的到达间隔
  │
  ▼
POST https://<models.dev返回的URL>/v1/chat/completions
  │
  ▼
SSE 流解析 → text-delta / reasoning-delta / tool-call 事件 → TUI 渲染
```

MiMo Code 有 **3 个** 与 MiMo 相关的 provider：

| Provider | API | npm | 认证 | 自定义 loader |
|----------|-----|-----|------|--------------|
| **mimo** | models.dev 动态下发 | `@ai-sdk/openai-compatible` | **无** | **无**（走纯默认路径） |
| **opencode** | `https://opencode.ai/zen/v1` | `@ai-sdk/openai-compatible` | `OPENCODE_API_KEY` | 有（未认证隐藏所有模型） |
| **xiaomi** | `https://api.xiaomimimo.com/v1` | `@ai-sdk/openai-compatible` | ECDH → sk | 无，有 auth plugin |

本文以 **`mimo` 免费模型** 为主线追踪。

---

## 2. 阶段一：models.dev 获取 API 地址

`mimo` provider 的 API URL 完全由 models.dev 动态下发，不在代码中硬编码。

### 2.1 请求

```http
GET /api.json HTTP/1.1
Host: models.dev
User-Agent: mimocode/<channel>/<version>/<MIMOCODE_CLIENT>
```

源码：`models.ts:122-128`
```ts
const fetchApi = async () => {
  const result = await fetch(`${url()}/api.json`, {
    headers: { "User-Agent": Installation.USER_AGENT },
    signal: AbortSignal.timeout(10000),
  })
  return { ok: result.ok, text: await result.text() }
}
```

User-Agent 构成（`installation/index.ts:58`）：
```
mimocode/<InstallationChannel>/<InstallationVersion>/<MIMOCODE_CLIENT>
```

默认值：`mimocode/local/local/cli`

### 2.2 加载优先级

源码 `models.ts:130-150`：
```
1. 本地缓存 (~/.cache/mimocode/models.json)        ← 优先
2. 构建时快照 (models-snapshot.js，仅发行版)         ← 次选
3. 网络获取 (Flock 文件锁保护)                       ← 最后
```

### 2.3 缓存策略

| 项目 | 值 |
|------|-----|
| TTL | 5 分钟 |
| 自动刷新间隔 | 60 分钟 |
| 锁机制 | Flock 文件锁 (state/locks/) |
| Hash 算法 | SHA-1（仅用于合法文件名生成） |

### 2.4 环境变量

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `MIMOCODE_MODELS_URL` | 覆盖 models.dev 地址 | `"https://models.dev"` |
| `MIMOCODE_MODELS_PATH` | 覆盖缓存路径 | `<cache>/models.json` |
| `MIMOCODE_DISABLE_MODELS_FETCH` | 禁止网络获取 | `false` |

### 2.5 响应格式

```json
{
  "mimo": {
    "name": "MiMo",
    "id": "mimo",
    "api": "https://<实际API域名>/v1",
    "npm": "@ai-sdk/openai-compatible",
    "env": [],
    "models": {
      "mimo-auto": {
        "id": "mimo-auto",
        "name": "MiMo Auto",
        "cost": { "input": 0, "output": 0 },
        "limit": { "context": 200000, "output": 32000 }
      }
    }
  }
}
```

**`mimo.providerID = "mimo"`，`mimo.model.api.url = <实际API域名>/v1`，`mimo.model.api.npm = "@ai-sdk/openai-compatible"`**

---

## 3. 阶段二：用户输入 → 消息创建

### 3.1 入口

源码：`prompt.ts:1770-1791`

```ts
const prompt = Effect.fn("SessionPrompt.prompt")(function* (input: PromptInput) {
  const session = yield* sessions.get(input.sessionID)
  // 1. 清理中断的 assistant 消息
  yield* revert.cleanup(session)
  yield* sweepOrphanAssistants(input.sessionID)
  // 2. 创建用户消息
  const message = yield* createUserMessage(input)
  // 3. 处理权限
  // 4. 进入 loop
  return yield* loop({ sessionID, agentID, task_id })
})
```

### 3.2 创建用户消息

源码：`prompt.ts:1368-1733`

```
createUserMessage(input):
  1. 解析 agent（默认 "main"）
  2. 解析 model（input.model > agent.model > lastModel）
  3. 解析 variant
  4. resolvePart() 处理每个 part：
     - type "text"  → 纯文本 part
     - type "file"  → Read tool 内联读取文件内容
     - type "agent" → 注入 subagent 调用提示
  5. 触发 plugin → "chat.message" hook
  6. 持久化到 SQLite
```

### 3.3 模型选择

源码：`provider.ts:1695-1732`

```ts
const defaultModel = Effect.fn("Provider.defaultModel")(function* () {
  // 1. config 中显式设置的 model
  if (cfg.model) return parseModel(cfg.model)
  // 2. 最近使用的模型（state/model.json）
  // 3. mimo/mimo-auto 作为最终回退
  const mimo = s.providers[ProviderID.make("mimo")]
  if (mimo?.models[ModelID.make("mimo-auto")]) {
    return { providerID: mimo.id, modelID: ModelID.make("mimo-auto") }
  }
})
```

**结论**：无配置时默认使用 `mimo/mimo-auto`。

---

## 4. 阶段三：Run Loop → LLM 调用准备

### 4.1 runLoop 结构

源码：`prompt.ts:1814-1910`

```
runLoop(sessionID, agentID):
  while true:
    1. 触发 plugin → "session.pre"
    2. 获取 agent 和 model
    3. 获取历史消息
    4. 触发 LLM.stream()
    5. 处理响应 → tool calls → 循环
    6. 触发 plugin → "session.post"
```

### 4.2 系统提示词构建

源码：`llm.ts:234-296`

```
buildSystemArray({ agent, model, system, user, sessionID }):
  system = [
    agent.prompt (或 SystemPrompt.provider(model)),   ← 默认 default.txt (13.9KB)
    ...input.system,                                    ← 调用方传入的自定义系统提示
    ...(user.system ? [user.system] : []),             ← 用户消息中的系统指令
  ].join("\n")

  如果不是系统生成的 actor（checkpoint-writer 等），追加：
    buildMemoryInstructions(sessionID, projectID)      ← ~80 行 Memory 系统指令

  触发 plugin → "experimental.chat.system.transform"
```

**`mimo-auto` 使用的系统提示词模板**：

```ts
// system.ts:20-34
function provider(model) {
  // mimo-auto / mimo-v2-* 不匹配任何特殊模型 → 使用 default.txt
  return [PROMPT_DEFAULT]  // session/prompt/default.txt (13.9KB)
}
```

### 4.3 环境信息注入

源码：`system.ts:50-70`

```
environment(model, now):
  "You are MiMo Code Agent, built by Xiaomi MiMo Team..."
  "You are powered by the model named mimo-auto."
  "The exact model ID is mimo/mimo-auto"
  <env>
    Working directory: /home/user/project
    Workspace root folder: /home/user/project
    Is directory a git repo: yes
    Platform: linux
    Today's date: Fri Jun 27 2026
  </env>
  "Your response must ALWAYS strictly follow the same major language as the user."
```

### 4.4 技能列表注入

源码：`system.ts:73-85`

```
skills(agent):
  // 从 Skill 注册表获取所有可用技能的 verbose 描述
  "Skills provide specialized instructions and workflows for specific tasks."
  "Use the skill tool to load a skill when a task matches its description."
  <verbose skill list>
```

### 4.5 完整系统提示词结构

```
[default.txt — 13.9KB]
  You are MiMoCode, an interactive CLI tool...
  # Tone and style
  ...
  # Tool usage
  ...

[环境信息]
  You are MiMo Code Agent, built by Xiaomi MiMo Team.
  You are powered by the model named mimo-auto.
  <env>...</env>

[技能列表]
  Skills provide specialized instructions...

[Memory 系统指令 — ~80 行]
  文件路径 / MEMORY.md 编辑规则 / Active recall protocol / Subagent return format
```

---

## 5. 阶段四：LLM.stream() → 参数与头构建

### 5.1 获取 Language Model

源码：`llm.ts:298-320`

```ts
const [language, cfg, item, info] = yield* Effect.all([
  provider.getLanguage(input.model),   // → resolveSDK() → 加载 @ai-sdk/openai-compatible
  config.get(),
  provider.getProvider(input.model.providerID),
  auth.get(input.model.providerID),
])
```

### 5.2 resolveSDK — 构建 Provider 实例

源码：`provider.ts:1429-1571`

```
resolveSDK(model, state, envs):
  options = { ...provider.options }
  
  // 对于 @ai-sdk/openai-compatible 且 includeUsage !== false:
  options["includeUsage"] = true
  
  // 解析 baseURL（优先级）:
  baseURL = options["baseURL"] || model.api.url
  // model.api.url = models.dev 返回的 api 字段
  
  // 如果 provider 有 key，注入
  if (options["apiKey"] === undefined && provider.key)
    options["apiKey"] = provider.key
  
  // 注入模型级 headers
  options["headers"] = { ...options["headers"], ...model.headers }
  
  // 自定义 fetch（包装 AbortSignal 合并 + SSE 超时）
  options["fetch"] = customFetchWrapper(model, options)
  
  // 加载 SDK
  const factory = await import("@ai-sdk/openai-compatible").createOpenAICompatible
  const sdk = factory({ name: "mimo", ...options })
```

### 5.3 chat.params hook

源码：`llm.ts:372-390`

所有注册的 plugin 可以修改参数。`mimo` provider 无任何 plugin 注册 `chat.params`。

默认参数：
```ts
{
  temperature: input.model.capabilities.temperature
    ? (agent.temperature ?? ProviderTransform.temperature(model))
    : undefined,                           // mimo → undefined（走 AI SDK 默认）
  topP: agent.topP ?? ProviderTransform.topP(model),   // → undefined
  topK: ProviderTransform.topK(model),     // → undefined
  maxOutputTokens: ProviderTransform.maxOutputTokens(model), // → 128000
  options: {
    ...ProviderTransform.options({         // → {} (mimo 无特殊 options)
        model, sessionID, providerOptions: item.options
      }),
    ...model.options,                      // → {} (mimo 无特殊 options)
    ...agent.options,                      // → {} (默认 agent 无特殊 options)
    ...variant,                            // → {} (无 variant 选中)
  },
}
```

### 5.4 chat.headers hook

源码：`llm.ts:392-404`

`mimo` provider 无任何 plugin 注册 `chat.headers`。最终 headers：
```ts
{
  "Content-Type": "application/json",           // AI SDK 自动
  "User-Agent": "mimocode/local/local/cli " +
                "ai-sdk/openai-compatible/0.1.0",  // SDK 拼接
  "x-session-affinity": "<sessionID>",          // llm.ts:603
}
```

**注意**：无 `Authorization` 头，无 `X-Mimo-Source` 头（仅 `xiaomi` provider 的 `plugin/mimo.ts` 注入）。

---

## 6. 阶段五：Provider Transform 层

### 6.1 message() — 消息预处理

源码：`transform.ts:450-486`

```ts
function message(msgs, model, options):
  msgs = unsupportedParts(msgs, model)     // 过滤不支持的内容类型
  msgs = limitImages(msgs)                  // 限制图片数量
  msgs = normalizeMessages(msgs, model)     // 空内容过滤（仅 anthropic/bedrock）
  if (supportsCacheMarkers(model)):
    msgs = applyCaching(msgs, model)        // mimo → false → 跳过
  remap providerOptions key                 // mimo → 跳过
```

**`mimo` provider 不受任何特殊处理**：
- `supportsCacheMarkers()` → `false`（`@ai-sdk/openai-compatible` 不在缓存支持列表）
- `normalizeMessages()` → 不处理（仅 anthropic/bedrock）
- key remap → 跳过（`sdkKey("@ai-sdk/openai-compatible")` → `undefined`）

### 6.2 tools() — Tool 缓存标记

源码：`transform.ts:495-505`

```ts
function tools(tools, model):
  if (supportsCacheMarkers(model)):  // mimo → false
    return withCacheMarkers(tools)
  return tools  // 直接返回
```

### 6.3 图片支持

源码：`transform.ts:23-28`

```ts
function supportsImageInput(model):
  if (model.providerID === "mimo" || model.providerID === "xiaomi"):
    if id.includes("v2.5-pro") → false
    if id === "mimo-auto" || id.includes("v2.5") → true
```

### 6.4 options() — 模型参数

源码：`transform.ts:948-1083`

```ts
function options({ model, sessionID }):
  result = {}
  // openai/provider-specific options → 不匹配 mimo
  // ...
  return result  // → {} 空对象
```

### 6.5 maxOutputTokens

源码：`transform.ts:1167-1172`

```ts
if (model.providerID === "mimo" || model.providerID === "xiaomi"):
  return 128_000
return min(model.limit.output, 32000) || 32000
```

### 6.6 providerOptions() — SDK 命名空间映射

源码：`transform.ts:1126-1165`

```ts
const key = sdkKey("@ai-sdk/openai-compatible") ?? "mimo"
// sdkKey 返回 undefined → key = "mimo"
return { [key]: options }  // { mimo: {} }
```

---

## 7. 阶段六：AI SDK 构建 HTTP 请求

### 7.1 createOpenAICompatible() 初始化

源码：`copilot-provider.ts:52-101`

```ts
function createOpenaiCompatible(options = {}):
  baseURL = withoutTrailingSlash(options.baseURL ?? "https://api.openai.com/v1")
  
  headers = {
    ...(options.apiKey && { Authorization: `Bearer ${options.apiKey}` }),
    ...options.headers,
  }
  
  getHeaders = () =>
    withUserAgentSuffix(headers, `ai-sdk/openai-compatible/${VERSION}`)
  
  createChatModel = (modelId) =>
    new OpenAICompatibleChatLanguageModel(modelId, {
      provider: "mimo.chat",
      headers: getHeaders,
      url: ({ path }) => `${baseURL}${path}`,
      fetch: options.fetch,
      includeUsage: options.includeUsage,  // true
    })
```

### 7.2 doStream() — 构建请求体

源码：`openai-compatible-chat-language-model.ts:305-723`

```ts
async doStream(options):
  // 1. 解析参数
  const { args, warnings } = await this.getArgs(options)
  
  // 2. 组装 body
  const body = {
    ...args,
    stream: true,
    stream_options: this.config.includeUsage
      ? { include_usage: true }
      : undefined,
  }
  
  // 3. 发送 POST
  const { responseHeaders, value: response } = await postJsonToApi({
    url: `${baseURL}/chat/completions`,
    headers: combineHeaders(this.config.headers(), options.headers),
    body,
    failedResponseHandler: ...,
    successfulResponseHandler: createEventSourceResponseHandler(chunkSchema),
    abortSignal: options.abortSignal,
    fetch: this.config.fetch,
  })
  
  // 4. pipe 响应流 → TransformStream → LanguageModelV3StreamPart
  return {
    stream: response.pipeThrough(chunkTransformStream),
    request: { body },
    response: { headers: responseHeaders },
  }
```

### 7.3 getArgs() — 构建 args 对象

源码：`openai-compatible-chat-language-model.ts:87-190`

```ts
async getArgs(options):
  // 解析 providerOptions
  compatibleOptions = parseProviderOptions({
    provider: "copilot",
    options.providerOptions,
    schema: openaiCompatibleProviderOptions,
  })
  // schema: { user, reasoningEffort, textVerbosity, thinking_budget }
  
  // 准备 tools
  const { tools: openaiTools, toolChoice, toolWarnings } =
    prepareTools({ tools, toolChoice })
  
  return {
    args: {
      model: this.modelId,                  // "mimo-auto"
      messages: convertMessages(prompt),     // 消息格式转换
      max_tokens: maxOutputTokens,           // 128000
      temperature,                           // undefined
      top_p: topP,                           // undefined
      frequency_penalty,                     // undefined
      presence_penalty,                      // undefined
      stop: stopSequences,                   // undefined
      seed,                                  // undefined
      tools: openaiTools,                    // [{ type: "function", function: {...} }]
      tool_choice: openaiToolChoice,         // "auto"
      reasoning_effort: compatibleOptions.reasoningEffort,  // undefined
      verbosity: compatibleOptions.textVerbosity,            // undefined
      thinking_budget: compatibleOptions.thinking_budget,    // undefined
      user: compatibleOptions.user,                          // undefined
      // + providerOptions 中的额外字段
    },
    warnings,
  }
```

---

## 8. 阶段七：消息格式转换

### 8.1 转换函数

源码：`convert-to-openai-compatible-chat-messages.ts:13-170`

**system 消息**：
```json
{ "role": "system", "content": "<完整系统提示词文本>" }
```

**user 消息**（纯文本）：
```json
{ "role": "user", "content": "<用户输入文本>" }
```

**user 消息**（多模态，含图片）：
```json
{
  "role": "user",
  "content": [
    { "type": "text", "text": "..." },
    { "type": "image_url", "image_url": { "url": "data:image/jpeg;base64,..." } }
  ]
}
```

**assistant 消息**：
```json
{
  "role": "assistant",
  "content": "<null 或文本>",
  "tool_calls": [
    {
      "id": "call_xxx",
      "type": "function",
      "function": { "name": "read", "arguments": "{\"file_path\":\"...\"}" }
    }
  ],
  "reasoning_text": "<思维链文本>",
  "reasoning_opaque": "<不透明引用>"
}
```

**tool 消息**：
```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "<工具执行结果文本>"
}
```

### 8.2 特殊字段

- `reasoning_text`：模型的思维链输出（仅当存在 `reasoning_opaque` 时才发送）
- `reasoning_opaque`：引用上一轮 thinking 块，实现多轮 reasoning 连续性
- `tool_calls`：仅在有 tool call 时存在，`assistant.content` 此时为 `null`

---

## 9. 阶段八：Tool 格式

### 9.1 Tool Schema 转换

源码：`transform.ts:1269-1376` + `openai-compatible-prepare-tools.ts`

```
ToolSchema → ProviderTransform.schema() → AI SDK tool() → prepareTools()
  │                                                     │
  │  1. 扁平化 anyOf/oneOf                              │
  │  2. 移除 $ref/$defs（内联）                          │
  │  3. 清理 additionalProperties                        │
  │  4. 移除顶层 enum restrictions（非 gemini/google）    │
  │                                                     │
  ▼                                                     ▼
{                                               [{
  type: "object",                                type: "function",
  properties: {...},                             function: {
  required: [...],                                 name: "read",
  additionalProperties: false                      description: "...",
}                                                  parameters: { type: "object", ... }
                                                 }
                                               }]
```

### 9.2 Tool 缓存标记

源码：`transform.ts:495-505`

对于 `mimo` provider：`supportsCacheMarkers` 返回 `false` → tools 不添加任何缓存标记。

---

## 10. 阶段九：自定义 fetch 包装

### 10.1 包装逻辑

源码：`provider.ts:1492-1529`

```ts
options["fetch"] = async (input, init?) => {
  fetchFn = customFetch ?? fetch    // 默认用原生 fetch

  // 1. 合并 AbortSignal
  signals = []
  if (opts.signal) signals.push(opts.signal)           // AI SDK 的请求级 signal
  if (chunkAbortCtl) signals.push(chunkAbortCtl.signal) // SSE chunk 超时 signal
  if (options["timeout"]) signals.push(AbortSignal.timeout(...)) // 全局超时
  combined = AbortSignal.any(signals)

  // 2. 对 @ai-sdk/openai provider：删除 input[].id（仅非 azure+store）
  //    mimo 是 @ai-sdk/openai-compatible → 不触发

  // 3. 实际发送
  res = await fetchFn(input, { ...opts, timeout: false })

  // 4. SSE 响应：包装 chunk 超时监控
  if (chunkAbortCtl) return wrapSSE(res, chunkTimeout, chunkAbortCtl)
  return res
}
```

### 10.2 SSE Chunk 超时

源码：`provider.ts:54-100`

```ts
const DEFAULT_CHUNK_TIMEOUT = 480_000  // 8 分钟

function wrapSSE(res, ms, ctl):
  // 对 SSE content-type 的响应：
  //   每个 ReadableStream chunk 读取设置 ms 超时
  //   超时 → abort 整个请求 → 由上层 persistentRetrySchedule 重试

  // 非 SSE 响应：直接返回
```

### 10.3 重试策略

源码：`llm.ts:74-79`

两层重试：

**外层**（llm.ts — 可见，TUI 显示 banner）：
```
persistentRetrySchedule:
  指数退避 500ms × 2
  每次延迟上限 5 分钟
  最多 11 次尝试（1 原始 + 10 重试）
  延迟序列: 0.5s, 1s, 2s, 4s, 8s, 16s, 32s, 64s, 128s, 256s
```

**内层**（AI SDK — 不可见）：
```
maxRetries: 2
```

---

## 11. 阶段十：完整 HTTP 请求示例

### 11.1 URL

```
POST https://<models.dev返回的mimo.api>/chat/completions
```

### 11.2 完整 Headers

```http
POST /v1/chat/completions HTTP/1.1
Host: <models.dev 返回的 host>
Content-Type: application/json
User-Agent: mimocode/local/local/cli ai-sdk/openai-compatible/0.1.0
x-session-affinity: 01JXXXXXXXXXXXXXXX
```

**无 `Authorization`，无 `X-Mimo-Source`。**

### 11.3 完整 Body（首轮对话，无历史）

```json
{
  "model": "mimo-auto",
  "messages": [
    {
      "role": "system",
      "content": "You are MiMoCode, an interactive CLI tool that helps users with software engineering tasks...[近 14KB 完整 default.txt]...\n\nYou are MiMo Code Agent, built by Xiaomi MiMo Team. You are an interactive agent that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.\nYou are powered by the model named mimo-auto. The exact model ID is mimo/mimo-auto\nHere is some useful information about the environment you are running in:\n<env>\n  Working directory: /home/user/project\n  Workspace root folder: /home/user/project\n  Is directory a git repo: yes\n  Platform: linux\n  Today's date: Fri Jun 27 2026\n</env>\nIMPORTANT: Your response must ALWAYS strictly follow the same major language as the user.\n\nSkills provide specialized instructions...[技能列表]...\n\n# Memory system\nYou have a persistent file-based memory system...[~80 行 Memory 指令]..."
    },
    {
      "role": "user",
      "content": "帮我写一个排序函数"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read",
        "description": "Read a file from the local filesystem...",
        "parameters": { "type": "object", "properties": {...}, "required": [...] }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "write",
        "description": "Write a file to the local filesystem...",
        "parameters": {...}
      }
    },
    ...
    {
      "type": "function",
      "function": {
        "name": "bash",
        "description": "Execute a bash command...",
        "parameters": {...}
      }
    }
  ],
  "tool_choice": "auto",
  "stream": true,
  "stream_options": { "include_usage": true },
  "max_tokens": 128000
}
```

### 11.4 多轮对话时的 Body（有历史 + 有 tool calls）

```json
{
  "model": "mimo-auto",
  "messages": [
    { "role": "system", "content": "<系统提示词>" },
    { "role": "user", "content": "帮我写一个排序函数" },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_abc123",
          "type": "function",
          "function": { "name": "write", "arguments": "{\"file_path\":\"/path/to/sort.py\",\"content\":\"def sort...\"}" }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc123",
      "content": "Successfully wrote file."
    },
    {
      "role": "assistant",
      "content": "已创建排序函数文件 sort.py。"
    }
  ],
  "stream": true,
  "stream_options": { "include_usage": true },
  "max_tokens": 128000
}
```

---

## 12. 阶段十一：SSE 响应解析

### 12.1 Chunk 结构

每个 SSE chunk 是一个 JSON 对象：

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion.chunk",
  "created": 1751234567,
  "model": "mimo-auto",
  "choices": [
    {
      "index": 0,
      "delta": {
        "role": "assistant",
        "content": "delta text",
        "reasoning_text": "thinking text",
        "reasoning_opaque": "...",
        "tool_calls": [
          {
            "index": 0,
            "id": "call_xxx",
            "function": {
              "name": "read",
              "arguments": "{\"file"
            }
          }
        ]
      },
      "finish_reason": null
    }
  ],
  "usage": {
    "prompt_tokens": 5432,
    "completion_tokens": 123,
    "total_tokens": 5555,
    "prompt_tokens_details": { "cached_tokens": 5000 },
    "completion_tokens_details": { "reasoning_tokens": 50 }
  }
}
```

### 12.2 流式事件类型

源码：`openai-compatible-chat-language-model.ts:379-718`

| AI SDK Event | 触发条件 |
|-------------|---------|
| `stream-start` | 流开始 |
| `response-metadata` | 第一个 chunk（含 id/model/created） |
| `reasoning-start` | 首次出现 `delta.reasoning_text` |
| `reasoning-delta` | 每个含 `delta.reasoning_text` 的 chunk |
| `reasoning-end` | thinking 结束（开始 text 或 tool call 或 flush） |
| `text-start` | 首次出现 `delta.content` |
| `text-delta` | 每个含 `delta.content` 的 chunk |
| `text-end` | flush 时结束 text |
| `tool-input-start` | 新 tool call 的首次 delta |
| `tool-input-delta` | tool call arguments 的增量 |
| `tool-input-end` | tool call arguments JSON 可解析 |
| `tool-call` | tool call 完成 |
| `finish` | 流结束（含 finish_reason + usage） |
| `error` | 错误 chunk |

### 12.3 usage 字段

```json
{
  "prompt_tokens": 5432,
  "completion_tokens": 123,
  "total_tokens": 5555,
  "prompt_tokens_details": {
    "cached_tokens": 5000
  },
  "completion_tokens_details": {
    "reasoning_tokens": 50,
    "accepted_prediction_tokens": null,
    "rejected_prediction_tokens": null
  }
}
```

---

## 13. 伪装方案

### 13.1 需要精确复刻的要素

| 要素 | 精确值/来源 | 说明 |
|------|-----------|------|
| **API URL** | models.dev 动态下发 | `GET https://models.dev/api.json` → `mimo.api` |
| **model 参数** | models.dev 动态下发 | `mimo.models.<key>.id`（如 `mimo-auto`） |
| **system prompt** | `default.txt` + env + skills + memory | 需完整拼接 |
| **User-Agent** | `mimocode/<channel>/<version>/<client>` | 默认 `mimocode/local/local/cli` |
| **x-session-affinity** | 随机 ULID | `llm.ts:603` |
| **stream** | `true` | 固定 |
| **stream_options** | `{ "include_usage": true }` | `@ai-sdk/openai-compatible` 自动注入 |
| **max_tokens** | `128000` | `transform.ts:1168` |
| **temperature** | 不传（undefined） | AI SDK 默认 |
| **tool_choice** | `"auto"` | 有 tools 时 |
| **tools** | `[{ type: "function", function: {...} }]` | 标准 OpenAI function calling 格式 |
| **消息转换** | 精确按 `convertToOpenAICompatibleChatMessages` | 含 `reasoning_text`/`reasoning_opaque`/`tool_calls` |
| **图片格式** | `data:image/jpeg;base64,...` | image_url 格式 |

### 13.2 不需要的要素

- `Authorization` 头 — **不需要**
- `X-Mimo-Source` 头 — **不需要**（仅 xiaomi provider）
- `cache_control` / `cachePoint` — **不需要**（`supportsCacheMarkers` → false）
- `reasoning_effort` / `text_verbosity` — **不需要**（options() 返回空）
- ECDH 认证 — **不需要**（仅 xiaomi provider 的 plugin/mimo.ts）

### 13.3 复刻步骤

```
1. GET https://models.dev/api.json
   Header: User-Agent: mimocode/local/local/cli
   → 获取 mimo.api (实际 URL) + mimo.models

2. 构建 system prompt：
   a. 读 packages/opencode/src/session/prompt/default.txt（完整原文）
   b. 拼环境信息（按 system.ts:50-68 格式）
   c. 附加 Memory 指令（按 llm.ts:99-179 格式）

3. 构建请求：
   POST {mimo.api}/chat/completions
   Headers:
     Content-Type: application/json
     User-Agent: mimocode/local/local/cli ai-sdk/openai-compatible/0.1.0
     x-session-affinity: <random_ulid>
   Body:
     {
       "model": "mimo-auto",
       "messages": [
         { "role": "system", "content": "<步骤2的结果>" },
         { "role": "user", "content": "<用户消息>" }
       ],
       "stream": true,
       "stream_options": { "include_usage": true },
       "max_tokens": 128000
     }

4. 解析 SSE 流：
   每行 "data: {json}" → JSON.parse → 取 choices[0].delta.content

5. 提供 OpenAI 兼容代理，对外暴露 /v1/chat/completions
```

### 13.4 关键风险

- **API URL 可能变更**：models.dev 可随时更改 `mimo.api` 指向
- **免费模型可能取消**：`cost.input === 0` 的模型由 models.dev 控制
- **User-Agent 检测**：服务端可能校验 User-Agent 格式
- **速率限制**：免费模型可能有 IP/UA 级别的频率限制
- **System prompt 检测**：服务端可能分析 prompt 内容识别非 MiMo Code 客户端
