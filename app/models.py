"""合闸单状态机。

状态流转：
  STARTING --(回执超时/丢失)--> UNKNOWN          （两者都按峰值占用）
  STARTING/UNKNOWN --(查实已稳定)--> RUNNING      （改按稳定电流占用）
  任意活动状态 --(查实断电)--> RELEASED           （释放额度，终态）
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class OrderState(str, Enum):
    STARTING = "STARTING"    # 已受理、启动中（等回执或待查实），按峰值占用
    UNKNOWN = "UNKNOWN"      # 回执超时、结果未明，仍按峰值占用
    RUNNING = "RUNNING"      # 已查实稳定运行，按稳定电流占用
    RELEASED = "RELEASED"    # 已查实断电，额度已释放（终态）


ACTIVE_STATES = (OrderState.STARTING, OrderState.UNKNOWN, OrderState.RUNNING)


@dataclass
class Order:
    server_id: str
    occupied_amps: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: OrderState = OrderState.STARTING
    receipt: dict | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    history: list[dict] = field(default_factory=list)

    def log(self, event: str, detail: str) -> None:
        self.history.append({"ts": _now(), "event": event, "detail": detail})
        self.updated_at = self.history[-1]["ts"]
