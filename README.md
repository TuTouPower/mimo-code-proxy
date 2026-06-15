# mimo-code-proxy

MiMo 免费通道 → OpenAI 兼容接口，Docker 一键部署。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env 设置 MIMO_KEY (可选)
docker compose up -d
```

## 端点

| 路径 | 说明 |
|------|------|
| `GET /v1/models` | 模型列表 |
| `POST /v1/chat/completions` | 聊天补全 |
| `GET /v1/health` | 健康检查 |

## 使用

```bash
curl http://localhost:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-mimo-change-me' \
  -d '{"model":"mimo-auto","messages":[{"role":"user","content":"hello"}],"max_tokens":1000}'
```

Python:
```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8788/v1", api_key="sk-mimo-change-me")
print(client.chat.completions.create(
    model="mimo-auto",
    messages=[{"role":"user","content":"hello"}],
    max_tokens=1000,
).choices[0].message.content)
```
