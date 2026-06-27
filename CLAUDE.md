# mimo-code-proxy CLAUDE.md

## 项目概述

MiMo 免费通道 → OpenAI 兼容接口，Docker 部署。单端口对外，内部 N 个代理出口轮询负载均衡，每个出口独立指纹 + JWT。stdlib only，无外部依赖。

## 技术栈

- Python 3.13，仅标准库
- Docker / docker compose 部署
- 测试用 `unittest`（stdlib）

## 目录结构

```
src/mimo_code_proxy.py       # 核心代理脚本
tests/                       # 测试
config/                      # 配置文件
Dockerfile                   # Alpine 镜像
docker-compose.yml           # 本地部署
```

## 开发约定

- snake_case 命名
- 缩进 4 空格
- 禁止 print 调试，用 `log()` 函数（stderr）
- 不加外部依赖
- 改代码后运行 `python3 -m unittest discover -s tests -v`

## 测试

```bash
python3 -m unittest test_mimo_code_proxy -v
```

测试覆盖：
- 配置文件加载与验证
- MimoBackend 指纹生成与持久化（SHA256，按 name 隔离）
- JWT 解码、过期刷新、401/403 强制刷新
- RoundRobin 轮询正确性（含并发线程安全）
- 鉴权逻辑（无 KEY / 正确 KEY / 错误 KEY / 缺少 header）
- HTTP 端点：health（返回 backend 数）、models、chat completions
- 多 backend 错误回退（单失败轮询到下一个）
- 上游错误传播、404
- Guard prompt 注入（不重复）
- max_tokens 钳制
- model 强制 mimo-auto
- 流式 / 非流式响应转发

## 配置

配置文件：`mimo_config.json`（或通过 `MIMO_CONFIG` 环境变量指定路径）

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
| `api_key` | API 密钥，空则无鉴权 |
| `backends` | 后端列表，每个 backend 独立指纹 + JWT |
| `backends[].name` | backend 标识名（用于日志和指纹文件） |
| `backends[].proxy` | HTTP/HTTPS 代理地址，`null` 表示直连 |
| `fingerprint_dir` | 指纹持久化目录 |

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MIMO_CONFIG` | `./mimo_config.json` | 配置文件路径 |
| `MIMO_LOG_LEVEL` | INFO | 日志级别 DEBUG/INFO/WARN/ERROR |
| `MIMO_FREE_BASE_URL` | https://api.xiaomimimo.com | 上游地址 |

⚠️ DEBUG 级别会把用户对话正文写入 stderr 日志，生产环境应调回 INFO 避免隐私泄露。

## Docker

```bash
cp mimo_config.example.json mimo_config.json
# 编辑配置
docker compose up -d
```
