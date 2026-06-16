# mimo-code-proxy

利用 [MiMo Code](https://github.com/XiaomiMiMo/MiMo-Code) 的免费 `mimo-auto` API，转换为 OpenAI / Anthropic 兼容接口。单端口对外，多代理出口轮询负载均衡，每个出口独立指纹 + JWT。

[English](README_en.md)

## 为什么多出口？

MiMo 的免费通道按**源 IP 限流**，不是按 key 或指纹。同一台机器上多个 key/指纹**无法叠加并发**。唯一扩容方式是**多出口 IP**。

本代理把这个问题包了一层：你对外只暴露一个端口，内部轮询分发到 N 个代理出口，每个出口有独立指纹和 JWT，彼此完全隔离。

## 架构

```
客户端 ──→ :8788 ──┬─→ backend sg-01 (代理: 7890, 指纹: sha256-1, JWT-A)
                   ├─→ backend jp-01 (代理: 7891, 指纹: sha256-2, JWT-B)
                   ├─→ backend us-01 (代理: 7892, 指纹: sha256-3, JWT-C)
                   └─→ backend direct (直连, 指纹: sha256-4, JWT-D)
```

- **轮询分发**：请求按顺序发到各个 backend
- **独立隔离**：每个 backend 的指纹文件、JWT 完全独立
- **自动回退**：某个 backend 失败，自动换下一个，直到全部试过
- **JWT 自维护**：过期前自动刷新，401/403 强制刷新重试
- **429 自动换指纹**：限流时自动生成新指纹重新 bootstrap

## 快速开始

```bash
# 1. 配置
cp mimo_config.example.json mimo_config.json
# 编辑 mimo_config.json，填入你的代理地址

# 2. Docker 启动
docker compose up -d

# 3. 测试 OpenAI 格式
curl http://localhost:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-mimo-change-me' \
  -d '{"model":"mimo-auto","messages":[{"role":"user","content":"hello"}]}'

# 测试 Anthropic 格式
curl http://localhost:8788/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: sk-mimo-change-me' \
  -d '{"model":"mimo-auto","max_tokens":200,"messages":[{"role":"user","content":"hello"}]}'
```

## 配置

`mimo_config.json`：

```json
{
    "listen": {"host": "0.0.0.0", "port": 8788},
    "api_key": "sk-mimo-change-me",
    "backends": [
        {"name": "sg-01", "proxy": "http://127.0.0.1:7890"},
        {"name": "jp-01", "proxy": "http://127.0.0.1:7891"},
        {"name": "direct", "proxy": null}
    ],
    "fingerprint_dir": "/data/mimo_fingerprints"
}
```

| 字段 | 说明 |
|------|------|
| `listen` | 监听地址和端口 |
| `api_key` | API 密钥，空字符串表示不鉴权 |
| `backends` | 后端列表，每个 backend 独立指纹 + JWT |
| `backends[].name` | 标识名，用于日志和指纹文件名 |
| `backends[].proxy` | HTTP/HTTPS 代理地址，`null` 表示直连 |
| `fingerprint_dir` | 指纹持久化目录，自动创建 |

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MIMO_CONFIG` | `./mimo_config.json` | 配置文件路径 |
| `MIMO_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARN`/`ERROR` |
| `MIMO_FREE_BASE_URL` | `https://api.xiaomimimo.com` | 上游地址 |

⚠️ `DEBUG` 级别会记录用户对话正文到 stderr，生产环境务必设为 `INFO`。

## 本地运行（不 Docker）

```bash
python3 mimo_code_proxy.py -c mimo_config.json
```

依赖：Python 3.10+，仅标准库。

## 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/models` | GET | 模型列表（固定返回 `mimo-auto`） |
| `/v1/chat/completions` | POST | 聊天补全（**OpenAI 兼容**） |
| `/v1/messages` | POST | 聊天补全（**Anthropic Messages API 兼容**） |
| `/v1/health` | GET | 健康检查，返回 backend 数量 |

### Anthropic 格式说明

- 鉴权：`x-api-key` header（兼容 Claude SDK）
- `system` 字段自动转为 OpenAI `system` role
- 流式：OpenAI SSE chunks → Anthropic 事件流（`message_start` → `content_block_delta` → `message_stop`）
- 非流式：返回标准 Anthropic message 响应结构

## 代理地址从哪来？

你需要的代理是**出站代理**（不是入站代理），常见来源：
- 机场/代理池（如 clash、mihomo、v2ray 的本地 socks5/http 端口）
- 各 VPS 上的 tinyproxy、squid
- 云服务商的转发代理

把它们的本地监听地址填到 `backends[].proxy` 即可。

## 测试

```bash
python3 -m unittest test_mimo_code_proxy -v
```

47 个测试，覆盖配置加载、指纹生成、JWT 刷新、轮询分发、错误回退、429 指纹旋转、Anthropic 格式转换、端到端 HTTP。

## 技术约束

- Python 3.10+，仅标准库（无 pip）
- 仅支持 HTTP/HTTPS 代理（SOCKS5 需要外部依赖）
- `mimo-auto` 是 reasoning 模型，建议 `max_tokens` ≥ 200

## License

MIT
