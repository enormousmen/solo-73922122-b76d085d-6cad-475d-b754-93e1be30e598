"""PDU 模拟器（独立 FastAPI 服务）。

可通过 /pdu/admin/mode/{server_id}?mode=... 为每台服务器设置行为：
- normal        : 正常合闸并在 PDU_RECEIPT_DELAY 秒后返回回执（默认）
- lost-receipt  : 实际合闸成功，但回执永不返回（客户端超时）——复现"合闸成功却丢回执"
- fail          : 直接返回 500，模拟 PDU/通信故障
- flaky         : 每次随机选上述三种行为

合闸后持续通电 PDU_BOOT_SECONDS 秒，/pdu/status 才报 stable=true。
"""
from __future__ import annotations

import asyncio
import os
import random
import time
import uuid

from fastapi import FastAPI, HTTPException

BOOT_SECONDS = float(os.environ.get("PDU_BOOT_SECONDS", "5"))
RECEIPT_DELAY = float(os.environ.get("PDU_RECEIPT_DELAY", "0.2"))
DEFAULT_MODE = os.environ.get("PDU_DEFAULT_MODE", "normal")
VALID_MODES = {"normal", "lost-receipt", "fail", "flaky"}

app = FastAPI(title="PDU 模拟器", version="1.0.0")

# server_id -> {"powered": bool, "on_since": float | None}
outlets: dict[str, dict] = {}
# server_id -> 行为模式覆盖
modes: dict[str, str] = {}


def _outlet(server_id: str) -> dict:
    return outlets.setdefault(server_id, {"powered": False, "on_since": None})


def _status(server_id: str) -> dict:
    outlet = _outlet(server_id)
    powered = bool(outlet["powered"])
    uptime = round(time.time() - outlet["on_since"], 2) if powered and outlet["on_since"] else 0.0
    return {
        "server_id": server_id,
        "powered": powered,
        "stable": powered and uptime >= BOOT_SECONDS,
        "uptime_seconds": uptime,
        "boot_seconds_required": BOOT_SECONDS,
    }


@app.post("/pdu/close/{server_id}")
async def close_outlet(server_id: str) -> dict:
    mode = modes.get(server_id, DEFAULT_MODE)
    if mode == "flaky":
        mode = random.choice(["normal", "lost-receipt", "fail"])

    if mode == "fail":
        raise HTTPException(status_code=500, detail="PDU 内部错误（模拟故障）")

    outlet = _outlet(server_id)
    if not outlet["powered"]:  # 重复合闸不重置通电计时
        outlet["powered"] = True
        outlet["on_since"] = time.time()

    if mode == "lost-receipt":
        # 关键行为：电已经送上，但回执丢弃，永远不响应本次请求
        await asyncio.sleep(3600)
        return {"unreachable": True}  # 实际不会到达

    await asyncio.sleep(RECEIPT_DELAY)
    return {
        "receipt_id": uuid.uuid4().hex[:8],
        "server_id": server_id,
        "outlet": server_id,
        "powered": True,
        "ts": time.time(),
    }


@app.post("/pdu/open/{server_id}")
async def open_outlet(server_id: str) -> dict:
    outlet = _outlet(server_id)
    outlet["powered"] = False
    outlet["on_since"] = None
    return {"receipt_id": uuid.uuid4().hex[:8], "server_id": server_id, "powered": False, "ts": time.time()}


@app.get("/pdu/status/{server_id}")
async def outlet_status(server_id: str) -> dict:
    return _status(server_id)


@app.get("/pdu/outlets")
async def all_outlets() -> dict:
    return {sid: _status(sid) for sid in outlets}


@app.post("/pdu/admin/mode/{server_id}")
async def set_mode(server_id: str, mode: str) -> dict:
    if mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode 必须是 {sorted(VALID_MODES)} 之一")
    modes[server_id] = mode
    return {"server_id": server_id, "mode": mode}


@app.get("/pdu/admin/mode")
async def get_modes() -> dict:
    return {"default": DEFAULT_MODE, "overrides": modes}


@app.get("/pdu/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
