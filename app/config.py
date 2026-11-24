"""服务配置：支路上限与各服务器的启动峰值/稳定电流。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServerConfig:
    id: str
    peak_amps: float    # 启动峰值电流
    stable_amps: float  # 稳定运行电流


@dataclass(frozen=True)
class AppConfig:
    branch_name: str
    limit_amps: float               # 支路允许承载的总电流上限
    receipt_timeout_seconds: float  # 等 PDU 回执的超时时间
    pdu_base_url: str
    servers: dict[str, ServerConfig]


def _default_config_path() -> str:
    candidates = [
        os.environ.get("APP_CONFIG", ""),
        "config/servers.json",
        str(Path(__file__).resolve().parent.parent / "config" / "servers.json"),
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise FileNotFoundError(f"找不到配置文件，尝试过: {candidates}")


def load_config(path: str | None = None) -> AppConfig:
    with open(path or _default_config_path(), encoding="utf-8") as fh:
        raw = json.load(fh)
    servers: dict[str, ServerConfig] = {}
    for item in raw["servers"]:
        peak, stable = float(item["peak_amps"]), float(item["stable_amps"])
        if peak <= 0 or stable <= 0 or peak < stable:
            raise ValueError(
                f"服务器 {item['id']} 电流配置非法: peak={peak}, stable={stable}"
            )
        servers[item["id"]] = ServerConfig(id=item["id"], peak_amps=peak, stable_amps=stable)
    return AppConfig(
        branch_name=raw["branch"]["name"],
        limit_amps=float(raw["branch"]["limit_amps"]),
        receipt_timeout_seconds=float(raw.get("receipt_timeout_seconds", 3.0)),
        pdu_base_url=os.environ.get(
            "PDU_BASE_URL", raw.get("pdu_base_url", "http://localhost:9000")
        ),
        servers=servers,
    )
