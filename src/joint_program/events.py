"""只追加的事件定义。

所有状态变化都先落为一条带前向哈希链的事件,再由聚合/投影重放得到。
事件一经写入不可修改;进程重启后从事件日志重放即可恢复全部待确认事项。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class Event:
    seq: int
    event_type: str
    at: datetime
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    causation_id: Optional[str] = None  # 触发本事件的提醒/升级任务id

    def to_line(self) -> str:
        data = {
            "seq": self.seq,
            "event_type": self.event_type,
            "at": self.at.isoformat(),
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "causation_id": self.causation_id,
        }
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_line(cls, line: str) -> "Event":
        data = json.loads(line)
        return cls(
            seq=data["seq"],
            event_type=data["event_type"],
            at=datetime.fromisoformat(data["at"]),
            payload=data["payload"],
            prev_hash=data["prev_hash"],
            hash=data["hash"],
            causation_id=data.get("causation_id"),
        )


def compute_hash(seq: int, event_type: str, at: datetime, payload: dict[str, Any], prev_hash: str) -> str:
    body = json.dumps(
        {
            "seq": seq,
            "event_type": event_type,
            "at": at.isoformat(),
            "payload": payload,
            "prev_hash": prev_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
