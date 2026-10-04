"""启动只读查询端点。

用法:

    python -m joint_program.serve <events.jsonl路径> --agreement A-JP-2026F --port 8080

端点:
    GET /health
    GET /current
    GET /obligations?party=P-B&cohort=COHORT-2026F
    GET /history?as_of=2026-09-12T09:00:00&cohort=COHORT-2026F
    GET /decisions?as_of=2026-09-12T09:00:00
"""
from __future__ import annotations

import argparse

from .http_api import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="联合培养履约协同只读查询端点")
    parser.add_argument("store_path", help="事件日志 JSONL 路径")
    parser.add_argument("--agreement", default="A-JP-2026F", help="协议标识")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = run_server(args.host, args.port, args.store_path, args.agreement)
    print(f"查询端点已启动: http://{args.host}:{args.port} (协议 {args.agreement})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
