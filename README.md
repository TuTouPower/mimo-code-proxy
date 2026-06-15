# mimo-code-proxy

MiMo 免费通道 → OpenAI 兼容接口，Docker 一键部署。支持多实例（每端口独立指纹）。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env 设置各实例 KEY（可选）
docker compose up -d
```

启动 3 个实例，分别监听 8788 / 8789 / 8790。

## 多实例架构

每个容器有独立的 UUID 指纹（`MIMO_CLIENT_ID`），MiMo 服务端视为不同客户端。可按需增加实例。

```
客户端 ──→ :8788 (mimo-1, fp=uuid-1) ──→ api.xiaomimimo.com
         → :8789 (mimo-2, fp=uuid-2) ──→ api.xiaomimimo.com
         → :8790 (mimo-3, fp=uuid-3) ──→ api.xiaomimimo.com
```

## 端点

| 路径 | 说明 |
|------|------|
| `GET /v1/models` | 模型列表 |
| `POST /v1/chat/completions` | 聊天补全 |
| `GET /v1/health` | 健康检查 |

## 配置

| 变量 | 说明 |
|------|------|
| `MIMO_CLIENT_ID` | 指纹，不设则自动生成 UUID 并持久化 |
| `MIMO_KEY` | API 密钥，空则无鉴权 |
| `MIMO_PORT` | 容器内端口（默认 8788） |
| `MIMO_UPSTREAM` | 上游地址 |

## 使用

```bash
curl http://localhost:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-mimo-change-me' \
  -d '{"model":"mimo-auto","messages":[{"role":"user","content":"hello"}],"max_tokens":1000}'
```
