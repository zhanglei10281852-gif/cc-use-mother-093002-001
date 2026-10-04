"""可控制的业务时钟：提醒与升级全部以它为时间基准。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """业务时钟协议，履约期限、会签有效期、提醒升级都通过它取当前时刻。"""

    def now(self) -> datetime: ...


class SystemClock:
    """真实时钟（UTC）。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """可手动推进的时钟，用于测试与事前演练。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock 需要带时区的起始时刻")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("时刻必须带时区")
        self._now = moment

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now
