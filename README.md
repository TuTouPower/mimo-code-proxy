# mimo-code-proxy

MiMo 免费通道 → OpenAI 兼容接口。单端口对外，内部 N 个代理出口轮询负载均衡，每个出口独立指纹 + JWT。

## 快速开始

```bash
cp mimo_config.example.json mimo_config.json
# 编辑 mimo_config.json，配置 backends
docker compose up -d
```

## 架构

```
客户端 ──→ :8788 ──┬─→ backend sg-01 (代理: 7890, 指纹: sha256-1)
                   ├─→ backend jp-01 (代理: 7891, 指纹: sha256-2)
                   ├─→ backend us-01 (代理: 7892, 指纹: sha256-3)
                   └─→ backend direct (直连, 指纹: sha256-4)
```

请求轮询分发，每个 backend 独立指纹 + JWT，互不影响。单 backend 失败自动回退到下一个。

## 端点

| 路径 | 说明 |
|------|------|
| `GET /v1/models` | 模型列表 |
| `POST /v1/chat/completions` | 聊天补全 |
| `GET /v1/health` | 健康检查（返回 backend 数量） |

## 配置

见 `mimo_config.example.json`：

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
| `api_key` | API 密钥，空则无鉴权 |
| `backends` | 后端列表，每个独立指纹 + JWT |
| `backends[].name` | 标识名 |
| `backends[].proxy` | HTTP/HTTPS 代理，`null` 表示直连 |

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MIMO_CONFIG` | `./mimo_config.json` | 配置文件路径 |
| `MIMO_LOG_LEVEL` | INFO | DEBUG/INFO/WARN/ERROR |
| `MIMO_FREE_BASE_URL` | https://api.xiaomimimo.com | 上游地址 |

## 使用

```bash
curl http://localhost:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-mimo-change-me' \
  -d '{"model":"mimo-auto","messages":[{"role":"user","content":"hello"}],"max_tokens":1000}'
```

## 测试

```bash
python3 -m unittest test_mimo_code_proxy -v
```
