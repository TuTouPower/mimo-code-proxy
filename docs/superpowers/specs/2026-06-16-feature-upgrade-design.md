# mimo-code-proxy 多后端轮询负载均衡设计

**日期**：2026-06-16
**状态**：待实现

## 目标

将 mimo-free-proxy 的多后端轮询负载均衡功能合并到本项目。

- 单端口对外，内部 N 个代理出口
- 每个出口独立指纹 + JWT，轮询分发请求
- 强制走新配置（JSON 文件），不向后兼容
- 约束不变：Python 3.13、仅标准库、snake_case、4 空格缩进、`log()` 写 stderr

## 范围

要做：
1. JSON 配置驱动
2. `MimoBackend` 类：每个 backend 独立指纹文件 + JWT + 代理出口
3. `RoundRobin` 类：轮询分发请求
4. 保留 req_id 日志追踪系统

不做：
- 向后兼容（单实例模式不再支持）
- 请求计数 / 限流

---

## 配置格式

`MIMO_CONFIG` 环境变量指定路径，默认 `./mimo_config.json`。

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

- `backends` 必填，至少 1 个
- `proxy` 为 `null` 表示直连（不经过代理）
- `fingerprint_dir` 存储各 backend 的指纹文件：`fp_{name}`

---

## 架构

### MimoBackend

每个 backend 实例独立管理：

| 职责 | 实现 |
|------|------|
| 指纹 | SHA256(name + hostname + platform + processor + random)，持久化到 `fp_{name}` |
| JWT | 独立存储 + 过期时间 + 线程锁 |
| 代理出站 | `urllib.request.ProxyHandler`（proxy 为 null 则直连） |
| JWT 刷新 | 过期前 5 分钟刷新；401/403 时强制刷新重试 |

### RoundRobin

线程安全的轮询选择器：
- 维护 backend 列表和索引
- `pick()` 返回下一个 backend（索引 +1 取模）
- 锁保护索引更新

### 请求处理流程

```
收到 chat 请求
  be = balancer.pick()
  try:
    resp = be.chat(payload)
    转发响应
  except 非 401/403 错误:
    记录失败，尝试下一个 backend
  except 401/403:
    be 强制刷新 JWT，重试当前 be
  所有 backend 都失败:
    返回最后一个错误
```

---

## 错误处理

| 场景 | 行为 |
|------|------|
| 单个 backend 请求失败（非 401/403） | 轮询到下一个 backend 重试 |
| 单个 backend 401/403 | 当前 backend 强制刷新 JWT，重试一次 |
| 所有 backend 都失败 | 返回最后一个错误 |
| 启动时 backend bootstrap 失败 | 记录错误，请求时重试 |

---

## 日志

保留现有 req_id 追踪系统，每条日志包含：
- `[req={req_id}]` 请求追踪
- `[{backend_name}]` backend 标识
- 原有日志级别和格式不变

---

## 接口兼容

对外 OpenAI 兼容接口不变：
- `GET /v1/models` → 模型列表
- `POST /v1/chat/completions` → 聊天
- `GET /health` → 返回 backend 数量 + 各 backend 状态

---

## 测试要点

沿用 `unittest`（stdlib）：

- 配置文件加载和验证（缺少 name、空 backends 等）
- MimoBackend 指纹生成和持久化
- MimoBackend JWT 过期刷新
- MimoBackend 401/403 强制刷新重试
- RoundRobin 轮询正确性（多线程并发）
- 多 backend 错误回退（一个失败换下一个）
- Handler 端到端（ThreadingHTTPServer 真测）
- `/health` 返回 backend 数量

---

## 风险与权衡

1. **不向后兼容** → 现有用户必须迁移到 JSON 配置
2. **仅支持 HTTP/HTTPS 代理** → SOCKS5 需要外部依赖（违反 stdlib only）
3. **SHA256 指纹 vs UUID** → 更"像"真实设备，但万一上游校验更严格可能失效
