"""只读查询端点(标准库 http.server)。

端点:
- GET /health
- GET /current                      当时钟时点适用的协议版本
- GET /obligations?party=&cohort=   各方未履约项(可按合作方/培养批次过滤)
- GET /history?as_of=ISO&cohort=    按历史时点与批次回放的完整报告
- GET /decisions?as_of=ISO          截至时点的可核验决策记录(哈希链)

端点只读:每次请求从事件日志重新载入并校验,保证读到的决策记录可核验。
"""
from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .domain import Clock, SystemClock
from .service import CollaborationService, _version_dict
from .store import EventStore


def build_handler(store_path: str, agreement_id: str, clock: Optional[Clock] = None):
    clock = clock or SystemClock()

    class QueryHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _service(self) -> CollaborationService:
            # 每请求重放:天然读到最新日志,且载入即完成哈希链校验。
            return CollaborationService(agreement_id, EventStore(store_path), clock)

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            try:
                if parsed.path == "/health":
                    self._send(200, {"status": "ok"})
                    return

                if parsed.path == "/current":
                    service = self._service()
                    self._send(200, {
                        "agreement_id": agreement_id,
                        "as_of": service.now.isoformat(),
                        "version": _version_dict(service.current_version()),
                    })
                    return

                if parsed.path == "/obligations":
                    service = self._service()
                    party = params.get("party", [None])[0]
                    cohort = params.get("cohort", [None])[0]
                    items = service.outstanding_obligations(party, cohort)
                    self._send(200, {
                        "agreement_id": agreement_id,
                        "as_of": service.now.isoformat(),
                        "cohort": cohort,
                        "party": party,
                        "outstanding": [o.as_dict() for o in items],
                    })
                    return

                if parsed.path == "/history":
                    raw = params.get("as_of", [None])[0]
                    if not raw:
                        self._send(400, {"error": "缺少 as_of 参数(ISO 8601)"})
                        return
                    as_of = datetime.fromisoformat(raw)
                    cohort = params.get("cohort", [None])[0]
                    service = self._service()
                    self._send(200, service.history_report(as_of, cohort))
                    return

                if parsed.path == "/decisions":
                    raw = params.get("as_of", [None])[0]
                    store = EventStore(store_path)
                    events = store.load()
                    if raw:
                        as_of = datetime.fromisoformat(raw)
                        events = [e for e in events if e.at <= as_of]
                    self._send(200, {
                        "agreement_id": agreement_id,
                        "count": len(events),
                        "decisions": [
                            {
                                "seq": e.seq,
                                "event_type": e.event_type,
                                "at": e.at.isoformat(),
                                "payload": e.payload,
                                "prev_hash": e.prev_hash,
                                "hash": e.hash,
                                "causation_id": e.causation_id,
                            }
                            for e in events
                        ],
                    })
                    return

                self._send(404, {"error": "未知端点"})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except FileNotFoundError as exc:
                self._send(404, {"error": str(exc)})

    return QueryHandler


def run_server(host: str, port: int, store_path: str, agreement_id: str) -> ThreadingHTTPServer:
    handler = build_handler(store_path, agreement_id)
    server = ThreadingHTTPServer((host, port), handler)
    return server
