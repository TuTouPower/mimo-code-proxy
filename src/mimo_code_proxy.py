#!/usr/bin/env python3
import argparse
import os

from src import constants, config, fingerprint, backend, handler


def main():
    p = argparse.ArgumentParser(description="MiMo 多出口代理轮询负载均衡器")
    p.add_argument("-c", "--config", default=os.environ.get(
        "MIMO_CONFIG", "./mimo_config.json"),
        help="配置文件路径")
    args = p.parse_args()

    cfg = config.load_config(args.config)
    backends_cfg = cfg.get("backends", [])
    api_key = cfg["api_key"]
    listen = cfg["listen"]
    fp_dir = cfg["fingerprint_dir"]

    backends = []
    for bc in backends_cfg:
        be = backend.MimoBackend(name=bc["name"], proxy_url=bc.get("proxy"), fingerprint_dir=fp_dir)
        fp = fingerprint.load_or_create_fingerprint(fp_dir, bc["name"])
        constants.log("INFO", f"[{be.name}] 就绪 (代理: {be.proxy_url or '直连'}, 指纹: {fp[:12]}...)")
        backends.append(be)

    balancer = backend.RoundRobin(backends)
    handler_cls = handler.make_handler(balancer, api_key)

    host, port = listen["host"], listen["port"]
    srv = handler.ThreadingHTTPServer((host, port), handler_cls)
    constants.log("INFO", f"监听 http://{host}:{port}/v1  上游={constants.CHAT_URL}  认证={'ON' if api_key else 'OFF'}  后端数={len(backends)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        constants.log("收到中断信号，退出")
        srv.shutdown()


if __name__ == "__main__":
    main()
