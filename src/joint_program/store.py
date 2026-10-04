"""追加式事件存储：JSONL 文件加哈希链，支撑重启恢复与可核验的决策记录。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .errors import StoreCorruptedError
from .hashing import GENESIS_HASH, canonical_json, sha256_text


class EventStore:
    """事件只追加、不修改；每条事件携带前一条的哈希，构成可核验链条。"""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._events = []
        if self._path.exists():
            self._events = self._read_and_verify()

    def _read_file(self) -> list:
        if not self._path.exists():
            return []
        return [
            json.loads(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _read_and_verify(self) -> list:
        events = self._read_file()
        self._verify_chain(events)
        return events

    @staticmethod
    def _record_hash(record: dict) -> str:
        body = {k: record[k] for k in ("seq", "event_type", "actor", "occurred_at", "payload", "prev_hash")}
        return sha256_text(record["prev_hash"] + "\n" + canonical_json(body))

    @classmethod
    def _verify_chain(cls, events: list) -> None:
        prev = GENESIS_HASH
        for expected_seq, record in enumerate(events, 1):
            if record.get("seq") != expected_seq:
                raise StoreCorruptedError(f"事件序号不连续：第 {expected_seq} 条")
            if record.get("prev_hash") != prev:
                raise StoreCorruptedError(f"事件哈希链在第 {expected_seq} 条处断裂")
            if cls._record_hash(record) != record.get("hash"):
                raise StoreCorruptedError(f"第 {expected_seq} 条事件内容被篡改")
            prev = record["hash"]

    def append(self, event_type: str, actor: str, occurred_at: datetime, payload: dict) -> dict:
        seq = len(self._events) + 1
        prev_hash = self._events[-1]["hash"] if self._events else GENESIS_HASH
        record = {
            "seq": seq,
            "event_type": event_type,
            "actor": actor,
            "occurred_at": occurred_at.isoformat(),
            "payload": payload,
            "prev_hash": prev_hash,
        }
        record["hash"] = self._record_hash(record)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._events.append(record)
        return dict(record)

    def read_all(self) -> list:
        return [dict(e) for e in self._events]

    def verify(self) -> bool:
        """重新读取磁盘文件并校验整条哈希链，用于发现外部篡改。"""
        try:
            self._verify_chain(self._read_file())
        except (StoreCorruptedError, json.JSONDecodeError, KeyError):
            return False
        return True
