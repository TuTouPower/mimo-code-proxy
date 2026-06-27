# mimo-code-proxy

Turn [MiMo Code](https://github.com/XiaomiMiMo/MiMo-Code)'s free `mimo-auto` API into OpenAI / Anthropic compatible endpoints. Single public port, multiple proxy backends with round-robin load balancing. Each backend has its own fingerprint and JWT, fully isolated.

[中文文档](README.md)

## Why Multiple Backends?

MiMo's free channel rate-limits by **source IP**, not by API key or fingerprint. Running multiple keys/fingerprints on the same machine **does not stack concurrency**.

The only way to scale is **multiple egress IPs**. This proxy wraps that complexity: you expose a single port to the world, while internally requests are distributed across N proxy backends, each with an independent fingerprint and JWT.

## Architecture

```
Client ──→ :8788 ──┬─→ backend sg-01 (proxy: 7890, fp: sha256-1, JWT-A)
                   ├─→ backend jp-01 (proxy: 7891, fp: sha256-2, JWT-B)
                   ├─→ backend us-01 (proxy: 7892, fp: sha256-3, JWT-C)
                   └─→ backend direct (direct, fp: sha256-4, JWT-D)
```

- **Round-robin**: Requests are distributed sequentially across backends
- **Full isolation**: Each backend has its own fingerprint file and JWT
- **Automatic fallback**: If one backend fails, the request tries the next until all are exhausted
- **Self-healing JWTs**: Auto-refresh before expiry; force-refresh on 401/403

## Quick Start

```bash
# 1. Configure
cp config/mimo_config.example.json mimo_config.json
# Edit mimo_config.json, fill in your proxy addresses

# 2. Start with Docker
docker compose up -d

# 3. Test OpenAI format
curl http://localhost:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-mimo-change-me' \
  -d '{"model":"mimo-auto","messages":[{"role":"user","content":"hello"}]}'

# Test Anthropic format
curl http://localhost:8788/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: sk-mimo-change-me' \
  -d '{"model":"mimo-auto","max_tokens":200,"messages":[{"role":"user","content":"hello"}]}'
```

## Configuration

`mimo_config.json`:

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

| Field | Description |
|-------|-------------|
| `listen` | Bind address and port |
| `api_key` | API key. Empty string = no auth |
| `backends` | Backend list. Each has its own fingerprint + JWT |
| `backends[].name` | Identifier. Used in logs and fingerprint filename |
| `backends[].proxy` | HTTP/HTTPS proxy address. `null` = direct |
| `fingerprint_dir` | Directory for fingerprint persistence. Auto-created |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIMO_CONFIG` | `./mimo_config.json` | Config file path |
| `MIMO_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARN`/`ERROR` |
| `MIMO_FREE_BASE_URL` | `https://api.xiaomimimo.com` | Upstream URL |

⚠️ `DEBUG` level logs user conversation content to stderr. Set to `INFO` in production.

## Local Run (No Docker)

```bash
python3 -m src.mimo_code_proxy -c mimo_config.json
```

Requirements: Python 3.10+, stdlib only.

## Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/v1/models` | GET | Model list (always returns `mimo-auto`) |
| `/v1/chat/completions` | POST | Chat completions (**OpenAI-compatible**) |
| `/v1/messages` | POST | Chat completions (**Anthropic Messages API-compatible**) |
| `/v1/health` | GET | Health check. Returns backend count |

### Anthropic Format Notes

- Auth: `x-api-key` header (compatible with Claude SDK)
- `system` field auto-converted to OpenAI `system` role
- Streaming: OpenAI SSE chunks → Anthropic event stream (`message_start` → `content_block_delta` → `message_stop`)
- Non-streaming: Returns standard Anthropic message response structure

## Where Do Proxy Addresses Come From?

You need **egress proxies** (not inbound proxies). Common sources:
- Airport/proxy pool (e.g. clash, mihomo, v2ray local socks5/http ports)
- Tinyproxy, squid running on various VPS
- Cloud provider forwarding proxies

### Recommended: mihomo Multi-Port Setup

We recommend [mihomo (Meta)](https://github.com/MetaCubeX/mihomo/tree/Meta) as the proxy frontend. Configure multiple HTTP ports via `listeners`, each bound to a different egress node:

```yaml
listeners:
  - name: sg-out
    type: http
    port: 7890
    proxy: sg-node
  - name: jp-out
    type: http
    port: 7891
    proxy: jp-node
  - name: us-out
    type: http
    port: 7892
    proxy: us-node
```

A single mihomo instance can provide multiple independent egress points. Just put the local addresses into `backends[].proxy`.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

36 tests covering config loading, fingerprint generation, JWT refresh, round-robin distribution, error fallback, parameter override, Anthropic format conversion, and end-to-end HTTP.

## Technical Constraints

- Python 3.10+, stdlib only (no pip)
- Only HTTP/HTTPS proxies (SOCKS5 requires external deps)
- `mimo-auto` is a reasoning model. Suggest `max_tokens` ≥ 200

## Acknowledgements

Thanks to the [Linux DO](https://linux.do/) community for promoting open-source projects.

## License

MIT
