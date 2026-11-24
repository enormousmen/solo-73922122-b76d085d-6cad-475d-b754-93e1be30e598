"""PDU HTTP 客户端：下发合闸/断电指令、查询插座状态。"""
from __future__ import annotations

import httpx


class HttpxPDUClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def close(self, server_id: str) -> dict:
        resp = await self._client.post(f"/pdu/close/{server_id}")
        resp.raise_for_status()
        return resp.json()

    async def open(self, server_id: str) -> dict:
        resp = await self._client.post(f"/pdu/open/{server_id}")
        resp.raise_for_status()
        return resp.json()

    async def status(self, server_id: str) -> dict:
        resp = await self._client.get(f"/pdu/status/{server_id}")
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
