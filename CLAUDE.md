# mimo-code-proxy CLAUDE.md

## 项目概述

MiMo 免费通道 → OpenAI 兼容接口，Docker 部署。stdlib only，无外部依赖。

## 技术栈

- Python 3.13，仅标准库
- Docker / docker compose 部署
- 测试用 `unittest`（stdlib）

## 目录结构

```
mimo_code_proxy.py       # 核心代理脚本
test_mimo_code_proxy.py  # 测试
Dockerfile               # Alpine 镜像
docker-compose.yml       # 本地部署
.env.example             # 配置模板
```

## 开发约定

- snake_case 命名
- 缩进 4 空格
- 禁止 print 调试，用 `log()` 函数（stderr）
- 不加外部依赖
- 改代码后运行 `python3 -m unittest test_mimo_code_proxy -v`

## 测试

```bash
python3 -m unittest test_mimo_code_proxy -v
```

测试覆盖：
- 指纹生成与缓存
- JWT 解码与过期
- 鉴权逻辑（无 KEY / 正确 KEY / 错误 KEY / 缺少 header）
- HTTP 端点：health、models、chat completions
- 上游错误传播、404
- Guard prompt 注入（不重复）
- max_tokens 钳制
- model 强制 mimo-auto

## 配置

环境变量（见 .env.example）：

| 变量 | 默认 | 说明 |
|------|------|------|
| MIMO_HOST | 0.0.0.0 | 监听地址 |
| MIMO_PORT | 8788 | 监听端口 |
| MIMO_KEY | (空) | API 密钥，空则无鉴权 |
| MIMO_UPSTREAM | https://api.xiaomimimo.com | 上游地址 |
| MIMO_CLIENT_FILE | /data/mimo-client | 指纹持久化路径 |

## Docker

```bash
cp .env.example .env
docker compose up -d
```
