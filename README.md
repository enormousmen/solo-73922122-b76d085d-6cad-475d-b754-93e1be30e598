# 单支路送电服务（FastAPI）

机房复电时，多台服务器同时启动的峰值电流可能让一条供电支路过载。
本服务模拟**单支路合闸送电的值班受理与额度管理**：预设支路上限和每台服务器的
启动峰值 / 稳定电流，值班员申请合闸，余量够才受理，受理即按峰值占用额度。

## 业务规则与状态机

```
                 申请合闸(余量够)
   OFF ────────────────────────────► STARTING ──回执正常──► STARTING(待查实稳定)
    ▲                                  │
    │                                  └──回执超时/丢失──► UNKNOWN(结果未明)
    │                                                        │
    │              按合闸单查实 PDU: 已通电且 stable          │
    │                                  ┌─────────────────────┘
    │                                  ▼
    │                               RUNNING  (占用改为稳定电流)
    │                                  │
    │              断电 + 按单查实 PDU: powered=false
    └──────────────────────────── RELEASED (释放额度，终态)
```

额度规则：

- `STARTING` / `UNKNOWN`：按该服务器**启动峰值**占用（回执丢了也保留峰值，宁紧勿松）；
- `RUNNING`：查实稳定后改按**稳定电流**占用，差额释放给支路；
- `RELEASED`：查实设备断电后占用归零；
- 支路剩余 = 上限 − 所有活动合闸单占用之和；
- 同一服务器存在活动单（STARTING/UNKNOWN/RUNNING）时重复申请，返回 **200 + 原单**，不重复占用；
- 查余量与开单在同一把 `asyncio.Lock` 临界区内，避免并发申请超额受理；
- 等 PDU 回执是后台异步任务，`close` 接口立即返回，**不阻塞其他服务器合闸**。

## 目录结构

```
.
├── app/                    # 送电服务
│   ├── main.py             # FastAPI 路由 + create_app（可注入 PDU 客户端）
│   ├── config.py           # config/servers.json 加载与校验
│   ├── models.py           # 合闸单 / 状态枚举
│   ├── store.py            # 内存台账、额度计算、状态流转、锁
│   ├── pdu_client.py       # httpx PDU 客户端
│   ├── requirements.txt
│   └── Dockerfile
├── pdu/                    # PDU 模拟器（独立 FastAPI 服务）
│   ├── main.py             # normal / lost-receipt / fail / flaky
│   ├── requirements.txt
│   └── Dockerfile
├── config/servers.json     # 支路上限 + 4 台服务器的峰值/稳定电流
├── tests/test_api.py       # 13 个关键流程测试（FakePDU，无需 Docker）
├── scripts/demo.sh         # 端到端演示（含丢回执、在途不阻塞）
└── docker-compose.yml
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

- API 服务：<http://localhost:8000>（Swagger 文档：<http://localhost:8000/docs>）
- PDU 模拟器：<http://localhost:9000>
- 支路配置通过卷挂载 `config/servers.json`，改完重启 `api` 即可。

## 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# 终端 1：PDU 模拟器
PDU_BOOT_SECONDS=5 uvicorn pdu.main:app --port 9000

# 终端 2：送电服务
PDU_BASE_URL=http://127.0.0.1:9000 \
APP_CONFIG=$PWD/config/servers.json \
uvicorn app.main:app --port 8000

# 终端 3：一键演示
bash scripts/demo.sh
```

## 配置：`config/servers.json`

示例：支路上限 `100A`，4 台服务器（峰值 30/30/40/25A，稳定 12/12/15/10A）。

```json
{
  "branch": { "name": "A-01", "limit_amps": 100.0 },
  "receipt_timeout_seconds": 3.0,
  "servers": [
    { "id": "srv-1", "peak_amps": 30.0, "stable_amps": 12.0 }
  ]
}
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/servers/{id}/close` | 申请合闸；201 新受理，200 重复申请返回原单，409 余量不足 |
| POST | `/api/servers/{id}/open` | 指令 PDU 断开该服务器插座 |
| POST | `/api/orders/{oid}/verify-stable` | 按本单向 PDU 查实已稳定 → 改按稳定电流计 |
| POST | `/api/orders/{oid}/release` | 查实设备已断电 → 释放额度；仍带电返回 409 |
| GET | `/api/branch` | **各机状态 + 支路占用/余量总览** |
| GET | `/api/orders` / `/api/orders/{oid}` | 合闸单列表 / 详情（含状态流转历史） |
| GET | `/api/health` | 健康检查 |

## API 调用示例（含“合闸成功却丢回执”）

```bash
API=http://localhost:8000
PDU=http://localhost:9000

# 1) 看初始支路
curl -s $API/api/branch | python3 -m json.tool

# 2) 申请合闸（受理即按 30A 峰值占用，立即返回合闸单号）
curl -s -X POST $API/api/servers/srv-1/close
# -> 201 {"order_id":"...","state":"STARTING","occupied_amps":30.0,...}

# 3) 余量不够时 -> 409，响应里带需求值与当前余量
curl -s -X POST $API/api/servers/srv-4/close

# 4) 重复申请 -> 200 + 同一个 order_id，"deduplicated": true
curl -s -X POST $API/api/servers/srv-1/close

# 5) 让 PDU 对 srv-4 模拟“合闸成功但丢回执”，再申请合闸
curl -s -X POST "$PDU/pdu/admin/mode/srv-4?mode=lost-receipt"
OID=$(curl -s -X POST $API/api/servers/srv-4/close | python3 -c 'import sys,json;print(json.load(sys.stdin)["order_id"])')

#    超过 receipt_timeout_seconds(3s) 后查询：state=UNKNOWN，25A 峰值仍占用
sleep 4; curl -s $API/api/orders/$OID

# 6) 按本次合闸单查实：PDU 显示实际已通电且 stable=true
#    （PDU 合闸后 PDU_BOOT_SECONDS 秒才报 stable，太早查会收到 409）
curl -s $PDU/pdu/status/srv-4
curl -s -X POST $API/api/orders/$OID/verify-stable
# -> state=RUNNING, occupied_amps=10.0（25A 峰值改为 10A 稳定电流）

# 7) 设备仍带电时不能释放
curl -s -X POST $API/api/orders/$OID/release        # 409

# 8) 断电 -> 查实 -> 释放
curl -s -X POST $API/api/servers/srv-4/open
curl -s -X POST $API/api/orders/$OID/release        # state=RELEASED, occupied_amps=0
```

## PDU 模拟器

| 接口 | 说明 |
|---|---|
| `POST /pdu/close/{server_id}` | 合闸并回回执；行为由模式决定 |
| `POST /pdu/open/{server_id}` | 断开插座 |
| `GET  /pdu/status/{server_id}` | `powered` / `stable` / 通电时长 |
| `GET  /pdu/outlets` | 所有插座状态 |
| `POST /pdu/admin/mode/{server_id}?mode=...` | 设置模拟模式 |

模式：

- `normal`（默认）：合闸后延迟 `PDU_RECEIPT_DELAY` 秒回正常回执；
- `lost-receipt`：**实际合闸并通电，但请求永不响应**，由 API 端超时判定为回执丢失；
- `fail`：直接返回 500；
- `flaky`：每次随机上述三种行为。

环境变量：`PDU_BOOT_SECONDS=5`（合闸后多久算稳定）、`PDU_RECEIPT_DELAY=0.2`、
`PDU_DEFAULT_MODE=normal`。

## 测试与实测结果

单元/流程测试（FakePDU，覆盖容量、幂等、丢回执、稳定、释放、404 等）：

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
# 13 passed
```

本机已用 Python 3.11 + FastAPI 0.165 + httpx 0.28 + uvicorn 0.54 实际起两个 uvicorn
进程并完整跑过 `scripts/demo.sh`，实测：

- 30+30+40A 三台受理后占用 100A，第 4 台（需 25A）收到 `409 remaining=0`；
- 查实稳定后占用降为 12+12+15=39A，余量 61A；
- `lost-receipt` 下 3s 后合闸单变 `UNKNOWN`，25A 峰值保留，重复申请返回同一单号；
- PDU 未稳定时 `verify-stable` 返回 409 及 PDU 实测状态，稳定后转 `RUNNING / 10A`；
- 带电时 `release` 返回 409；`open` 后再释放成功，占用归零；
- 丢回执场景下 `close` 接口实测 **0.015s 返回**（没有同步干等 3s 超时），
  回执在途的同一窗口内另一台服务器合闸返回 201；
- `GET /api/branch` 各机状态与余量计算始终一致（末态 12+30+15+25=82A，余量 18A）；
- `docker-compose.yml` 已通过 YAML 解析校验，OpenAPI schema 正常生成全部 8 个接口。

## 设计取舍 / 还没完成的事项

- **内存台账、单实例**：重启进程合闸单丢失，也不支持多副本。生产化需要换
  SQLite/Postgres 并处理多实例互斥。
- **没有真正的定时巡检任务**：UNKNOWN 单的后续状态依赖值班员调用
  `verify-stable` / `release` 查实；后台只负责等首轮回执。可加周期对账 PDU 的 worker。
- **没有鉴权/审计落库**：目前只有合闸单内的状态流转历史，没有操作员身份与权限。
- **Docker Compose 未在本环境实际 `up`**：当前环境没有 docker / podman，
  镜像构建与 compose 启动未实跑；Dockerfile 很薄且依赖版本即本机实测版本，
  但仍建议在有 Docker 的机器上执行一次 `docker compose up --build` 验收。
- **PDU 是模拟器**：真实 PDU 协议（SNMP/Modbus/厂商 HTTP）需要另写客户端适配，
  实现 `app/main.py` 里的 `PDUClient` 三个方法即可替换。
- 电流值按配置的固定峰值/稳定值记账，未采集 PDU 实时电流；PDU 的
  `stable` 目前用“通电时长 ≥ PDU_BOOT_SECONDS”近似。
