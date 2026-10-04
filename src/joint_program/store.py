"""只追加事件日志:JSONL 文件存储,带哈希链校验与崩溃恢复。

写入采用"先构造事件、顺序追加、立即 flush/fsync"的方式;
进程重启后调用 load() 重放即可恢复全部待确认事项。
哈希链在重放时逐事件校验,任何被覆盖/截断/插入的记录都会被发现。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .events import GENESIS_HASH, Event, compute_hash


class EventStore:
    def __init__(self, path: os.PathLike | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, events: Iterable[Event]) -> None:
        events = list(events)
        if not events:
            return
        # 全批拼成单次写入后 flush/fsync,保证一批事件要么整体可见、要么不可见。
        payload = "".join(f"{event.to_line()}\n" for event in events)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

    def load(self) -> list[Event]:
        if not self.path.exists():
            return []
        events: list[Event] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(Event.from_line(line))
                except Exception as exc:  # 含末尾半行等损坏
                    raise ValueError(
                        f"事件日志第 {line_no} 行无法解析(日志可能损坏或被截断): {exc}"
                    ) from exc
        self.verify(events)
        return events

    @staticmethod
    def verify(events: list[Event]) -> None:
        prev = GENESIS_HASH
        expected_seq = 1
        for event in events:
            if event.seq != expected_seq:
                raise ValueError(f"事件序号不连续: 第 {event.seq} 条(期望 {expected_seq})")
            if event.prev_hash != prev:
                raise ValueError(f"事件哈希链断裂: seq={event.seq}")
            digest = compute_hash(
                event.seq, event.event_type, event.at, event.payload, event.prev_hash
            )
            if digest != event.hash:
                raise ValueError(f"事件内容校验失败: seq={event.seq}")
            prev = event.hash
            expected_seq += 1

    def head_hash(self) -> Optional[str]:
        events = self.load()
        return events[-1].hash if events else None
