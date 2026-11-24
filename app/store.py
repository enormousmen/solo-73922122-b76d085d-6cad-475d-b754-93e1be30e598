"""支路台账：额度占用与合闸单生命周期（内存实现，单实例）。"""
from __future__ import annotations

import asyncio

from .config import AppConfig
from .models import ACTIVE_STATES, Order, OrderState


class BranchStore:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.orders: dict[str, Order] = {}
        # 保护“查余量 -> 开单占用”这一临界区，避免并发申请超额受理
        self.lock = asyncio.Lock()

    # ---------------- 额度 ----------------
    def occupied_amps(self) -> float:
        return round(
            sum(o.occupied_amps for o in self.orders.values() if o.state in ACTIVE_STATES),
            3,
        )

    def remaining_amps(self) -> float:
        return round(self.config.limit_amps - self.occupied_amps(), 3)

    # ---------------- 合闸单 ----------------
    def active_order_for(self, server_id: str) -> Order | None:
        """该服务器当前未终结的合闸单（启动中 / 结果未明 / 运行中）。"""
        for order in self.orders.values():
            if order.server_id == server_id and order.state in ACTIVE_STATES:
                return order
        return None

    def create_order(self, server_id: str) -> Order:
        peak = self.config.servers[server_id].peak_amps
        order = Order(server_id=server_id, occupied_amps=peak)
        order.log("ACCEPTED", f"受理合闸申请，按启动峰值 {peak}A 占用支路额度")
        self.orders[order.id] = order
        return order

    def mark_receipt(self, order_id: str, receipt: dict) -> None:
        order = self.orders[order_id]
        if order.state != OrderState.STARTING:
            return
        order.receipt = receipt
        order.log("RECEIPT_OK", "收到 PDU 合闸回执，设备启动中，继续按峰值占用")

    def mark_unknown(self, order_id: str, error: str) -> None:
        order = self.orders[order_id]
        if order.state != OrderState.STARTING:
            return
        order.state = OrderState.UNKNOWN
        order.error = error
        order.log("RECEIPT_TIMEOUT", f"回执超时/丢失，结果未明，峰值额度继续保留: {error}")

    def mark_running(self, order_id: str) -> None:
        order = self.orders[order_id]
        stable = self.config.servers[order.server_id].stable_amps
        order.state = OrderState.RUNNING
        order.occupied_amps = stable
        order.log("VERIFIED_STABLE", f"按本单查实设备已稳定运行，改按稳定电流 {stable}A 占用")

    def mark_released(self, order_id: str) -> None:
        order = self.orders[order_id]
        order.state = OrderState.RELEASED
        order.occupied_amps = 0.0
        order.log("RELEASED", "查实合闸终止且设备断电，释放支路额度")
