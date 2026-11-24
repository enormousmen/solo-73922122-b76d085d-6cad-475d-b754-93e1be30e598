"""关键流程测试：受理占用、容量拒绝、幂等返回原单、丢回执保留峰值、查实稳定、查实断电释放。"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig, ServerConfig
from app.main import create_app


class FakePDU:
    """可编程 PDU 替身：记录通电状态，可注入合闸回执异常。"""

    def __init__(self) -> None:
        self.powered: dict[str, bool] = {}
        self.stable: dict[str, bool] = {}
        self.close_errors: dict[str, Exception] = {}

    async def close(self, server_id: str) -> dict:
        if server_id in self.close_errors:
            raise self.close_errors[server_id]
        self.powered[server_id] = True
        return {"receipt_id": f"rcpt-{server_id}", "server_id": server_id, "powered": True}

    async def open(self, server_id: str) -> dict:
        self.powered[server_id] = False
        return {"server_id": server_id, "powered": False}

    async def status(self, server_id: str) -> dict:
        return {
            "server_id": server_id,
            "powered": self.powered.get(server_id, False),
            "stable": self.stable.get(server_id, False),
        }


CONFIG = AppConfig(
    branch_name="A-01",
    limit_amps=100.0,
    receipt_timeout_seconds=0.1,
    pdu_base_url="http://fake-pdu",
    servers={
        "srv-1": ServerConfig("srv-1", peak_amps=30.0, stable_amps=12.0),
        "srv-2": ServerConfig("srv-2", peak_amps=30.0, stable_amps=12.0),
        "srv-3": ServerConfig("srv-3", peak_amps=40.0, stable_amps=15.0),
        "srv-4": ServerConfig("srv-4", peak_amps=25.0, stable_amps=10.0),
    },
)


@pytest.fixture()
def env():
    pdu = FakePDU()
    client = TestClient(create_app(CONFIG, pdu))
    with client:  # 触发 lifespan（当前无额外逻辑，保持习惯）
        yield client, pdu


# ---------------- 受理与占用 ----------------

def test_close_accepted_occupies_peak(env):
    client, _ = env
    resp = client.post("/api/servers/srv-1/close")
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "STARTING"
    assert body["occupied_amps"] == 30.0
    assert body["deduplicated"] is False

    branch = client.get("/api/branch").json()
    assert branch["occupied_amps"] == 30.0
    assert branch["remaining_amps"] == 70.0

    # TestClient 会等后台任务结束，回执应已写入
    order = client.get(f"/api/orders/{body['order_id']}").json()
    assert order["receipt"]["receipt_id"] == "rcpt-srv-1"


def test_insufficient_capacity_rejected(env):
    client, _ = env
    for sid in ("srv-1", "srv-2", "srv-3"):  # 30+30+40 = 100A 正好占满
        assert client.post(f"/api/servers/{sid}/close").status_code == 201

    resp = client.post("/api/servers/srv-4/close")  # 还需 25A
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["remaining_amps"] == 0.0
    assert detail["required_peak_amps"] == 25.0

    # 被拒不产生占用
    branch = client.get("/api/branch").json()
    assert branch["occupied_amps"] == 100.0


# ---------------- 幂等：重复申请返回原单 ----------------

def test_duplicate_close_returns_original_order(env):
    client, _ = env
    first = client.post("/api/servers/srv-1/close").json()
    second = client.post("/api/servers/srv-1/close")
    assert second.status_code == 200
    body = second.json()
    assert body["order_id"] == first["order_id"]
    assert body["deduplicated"] is True
    assert client.get("/api/branch").json()["occupied_amps"] == 30.0  # 不重复占用


def test_duplicate_close_in_unknown_state_returns_original(env):
    client, pdu = env
    pdu.close_errors["srv-1"] = httpx.TimeoutException("simulated timeout")
    first = client.post("/api/servers/srv-1/close").json()
    stored = client.get(f"/api/orders/{first['order_id']}").json()
    assert stored["state"] == "UNKNOWN"

    second = client.post("/api/servers/srv-1/close").json()
    assert second["order_id"] == first["order_id"]
    assert second["deduplicated"] is True


# ---------------- 丢回执 / 结果未明 ----------------

def test_receipt_lost_marks_unknown_and_keeps_peak(env):
    client, pdu = env
    pdu.close_errors["srv-1"] = httpx.TimeoutException("simulated timeout")
    created = client.post("/api/servers/srv-1/close").json()

    order = client.get(f"/api/orders/{created['order_id']}").json()
    assert order["state"] == "UNKNOWN"
    assert order["occupied_amps"] == 30.0  # 峰值保留
    assert "TimeoutException" in order["error"]

    branch = client.get("/api/branch").json()
    assert branch["remaining_amps"] == 70.0


def test_other_servers_can_close_while_one_unknown(env):
    client, pdu = env
    pdu.close_errors["srv-1"] = httpx.TimeoutException("simulated timeout")
    client.post("/api/servers/srv-1/close")  # 30A，UNKNOWN

    resp = client.post("/api/servers/srv-2/close")  # 余量 70A，够 30A
    assert resp.status_code == 201
    assert client.get("/api/branch").json()["remaining_amps"] == 40.0


# ---------------- 查实稳定 ----------------

def test_verify_stable_switches_to_stable_current(env):
    client, pdu = env
    created = client.post("/api/servers/srv-1/close").json()
    pdu.stable["srv-1"] = True

    resp = client.post(f"/api/orders/{created['order_id']}/verify-stable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "RUNNING"
    assert body["occupied_amps"] == 12.0  # 30A 峰值 -> 12A 稳定

    branch = client.get("/api/branch").json()
    assert branch["occupied_amps"] == 12.0
    assert branch["remaining_amps"] == 88.0


def test_verify_stable_rejected_when_not_stable(env):
    client, _ = env
    created = client.post("/api/servers/srv-1/close").json()
    resp = client.post(f"/api/orders/{created['order_id']}/verify-stable")
    assert resp.status_code == 409
    assert "尚未稳定" in resp.json()["detail"]["error"]


# ---------------- 查实断电 + 释放 ----------------

def test_release_refused_while_powered_then_ok_after_open(env):
    client, _ = env
    created = client.post("/api/servers/srv-1/close").json()

    refused = client.post(f"/api/orders/{created['order_id']}/release")
    assert refused.status_code == 409
    assert "仍带电" in refused.json()["detail"]["error"]

    assert client.post("/api/servers/srv-1/open").status_code == 200
    released = client.post(f"/api/orders/{created['order_id']}/release")
    assert released.status_code == 200
    body = released.json()
    assert body["state"] == "RELEASED"
    assert body["occupied_amps"] == 0.0
    assert client.get("/api/branch").json()["remaining_amps"] == 100.0

    # 释放幂等
    again = client.post(f"/api/orders/{created['order_id']}/release")
    assert again.status_code == 200
    assert again.json()["state"] == "RELEASED"


def test_new_order_allowed_after_release(env):
    client, _ = env
    first = client.post("/api/servers/srv-1/close").json()
    client.post("/api/servers/srv-1/open")
    client.post(f"/api/orders/{first['order_id']}/release")

    second = client.post("/api/servers/srv-1/close")
    assert second.status_code == 201
    assert second.json()["order_id"] != first["order_id"]


# ---------------- 丢回执但实际合闸成功的完整链路 ----------------

def test_lost_receipt_but_actually_closed_full_flow(env):
    client, pdu = env
    # PDU 实际已合闸并稳定，但 close 回执超时
    pdu.close_errors["srv-1"] = httpx.TimeoutException("simulated timeout")
    pdu.powered["srv-1"] = True
    pdu.stable["srv-1"] = True

    created = client.post("/api/servers/srv-1/close").json()
    order = client.get(f"/api/orders/{created['order_id']}").json()
    assert order["state"] == "UNKNOWN"

    # 结果未明期间重复申请 -> 原单
    assert client.post("/api/servers/srv-1/close").json()["order_id"] == order["order_id"]

    # 按本次合闸单查实稳定 -> 转 RUNNING，按稳定电流计
    stable = client.post(f"/api/orders/{order['order_id']}/verify-stable").json()
    assert stable["state"] == "RUNNING"
    assert stable["occupied_amps"] == 12.0

    # 断电 -> 查实 -> 释放
    client.post("/api/servers/srv-1/open")
    released = client.post(f"/api/orders/{order['order_id']}/release").json()
    assert released["state"] == "RELEASED"
    assert client.get("/api/branch").json()["remaining_amps"] == 100.0


# ---------------- 查询与 404 ----------------

def test_branch_status_lists_all_servers(env):
    client, _ = env
    client.post("/api/servers/srv-1/close")
    branch = client.get("/api/branch").json()
    assert branch["branch"] == "A-01"
    assert branch["limit_amps"] == 100.0
    states = {s["server_id"]: s["state"] for s in branch["servers"]}
    assert states == {"srv-1": "STARTING", "srv-2": "OFF", "srv-3": "OFF", "srv-4": "OFF"}


def test_unknown_server_and_order_404(env):
    client, _ = env
    assert client.post("/api/servers/nope/close").status_code == 404
    assert client.post("/api/orders/nope/verify-stable").status_code == 404
    assert client.post("/api/orders/nope/release").status_code == 404
    assert client.get("/api/orders/nope").status_code == 404
