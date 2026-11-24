#!/usr/bin/env bash
# 端到端演示：支路上限 100A，含“合闸成功却丢回执”与“等回执不阻塞他人”场景。
# 用法：
#   docker compose up --build      （或本地分别启动 pdu/ 与 app/，见 README）
#   bash scripts/demo.sh
# 可用 API=... PDU=... 覆盖地址。建议对全新启动的服务运行。
set -euo pipefail

API=${API:-http://localhost:8000}
PDU=${PDU:-http://localhost:9000}

pretty() { python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))"; }

# 单行摘要；遇到错误信封也不崩
line() {
  python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('  (无响应/非 JSON)'); sys.exit(0)
if 'order_id' not in d:
    print('  ->', json.dumps(d.get('detail', d), ensure_ascii=False)); sys.exit(0)
print('  %-6s 状态=%-9s 占用=%-5sA 单号=%s 去重=%s' % (
    d.get('server_id'), d.get('state'), d.get('occupied_amps'),
    d.get('order_id'), d.get('deduplicated', '-')))
"
}

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

active_order() { # $1=server_id -> 打印活动单号
  curl -s "$API/api/branch" | python3 -c "import sys,json; sid='$1'; print([x['active_order_id'] for x in json.load(sys.stdin)['servers'] if x['server_id']==sid][0])"
}

step "1. 初始：支路余量与各机状态"
curl -s "$API/api/branch" | pretty

step "2. srv-1 / srv-2 / srv-3 申请合闸（峰值 30+30+40=100A，正好占满）"
for s in srv-1 srv-2 srv-3; do
  curl -s -X POST "$API/api/servers/$s/close" | line
done

step "3. srv-4 再申请（需 25A，余量 0）-> 409 不予受理，且不产生占用"
curl -s -X POST "$API/api/servers/srv-4/close" | pretty

step "4. 等待 PDU 报告稳定（PDU_BOOT_SECONDS=5s），按合闸单逐单查实稳定"
sleep 6
for s in srv-1 srv-2 srv-3; do
  oid=$(active_order "$s")
  curl -s -X POST "$API/api/orders/$oid/verify-stable" | line
done
curl -s "$API/api/branch" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  查实后：占用 {d['occupied_amps']}A / 上限 {d['limit_amps']}A / 余量 {d['remaining_amps']}A\")"

step "5. 把 srv-4 的 PDU 模式设为 lost-receipt（实际合闸成功，但回执丢失）"
curl -s -X POST "$PDU/pdu/admin/mode/srv-4?mode=lost-receipt" | pretty

step "6. srv-4 申请合闸：受理即按峰值 25A 占用；约 3s 后回执超时 -> UNKNOWN，峰值保留"
OID4=$(curl -s -X POST "$API/api/servers/srv-4/close" | python3 -c "import sys,json; print(json.load(sys.stdin)['order_id'])")
echo "  合闸单: $OID4"
sleep 4
curl -s "$API/api/orders/$OID4" | pretty

step "7. 结果未明期间重复申请 srv-4 -> 200 返回原单（同一单号，不重复占用）"
curl -s -X POST "$API/api/servers/srv-4/close" | line

step "8. 运行中的 srv-1 重复申请同样返回原单；查看支路总览（UNKNOWN 的 25A 仍占着）"
curl -s -X POST "$API/api/servers/srv-1/close" | line
curl -s "$API/api/branch" | pretty

step "9. 按 srv-4 原单查实稳定：若 PDU 还在启动窗口内会先收到 409；轮询到 stable 后查实成功"
curl -s -X POST "$API/api/orders/$OID4/verify-stable" | line || true
for _ in $(seq 1 10); do
  st=$(curl -s "$PDU/pdu/status/srv-4" | python3 -c "import sys,json; print(json.load(sys.stdin)['stable'])")
  [ "$st" = "True" ] && break
  sleep 1
done
curl -s -X POST "$API/api/orders/$OID4/verify-stable" | line

step "10. 设备仍带电时申请释放 -> 409 拒绝（额度不释放）"
curl -s -X POST "$API/api/orders/$OID4/release" | pretty

step "11. 指令 PDU 断开 srv-4，再查实断电 -> RELEASED，释放 10A"
curl -s -X POST "$API/api/servers/srv-4/open" | pretty
curl -s -X POST "$API/api/orders/$OID4/release" | line

step "12. 回执在途不阻塞：srv-4 再次丢回执合闸，close 接口必须毫秒级返回 STARTING"
curl -s -X POST "$PDU/pdu/admin/mode/srv-4?mode=lost-receipt" >/dev/null
t0=$(date +%s.%N)
pending=$(curl -s -X POST "$API/api/servers/srv-4/close")
t1=$(date +%s.%N)
echo "$pending" | line
OID4B=$(echo "$pending" | python3 -c "import sys,json; print(json.load(sys.stdin)['order_id'])")
python3 -c "print(f'  close 接口耗时 {$t1-$t0:.3f}s（回执超时阈值 3s，证明没有同步干等回执）')"
curl -s "$API/api/orders/$OID4B" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  立刻查询：state={d['state']}, receipt={d['receipt']}（回执仍在途）\")"

step "13. 同一等待窗口内，让 srv-2 断电释放后重新合闸 -> 余量够，照常 201 受理"
OID2=$(active_order srv-2)
curl -s -X POST "$API/api/servers/srv-2/open" >/dev/null
curl -s -X POST "$API/api/orders/$OID2/release" | line
curl -s -X POST "$PDU/pdu/admin/mode/srv-2?mode=normal" >/dev/null
curl -s -X POST "$API/api/servers/srv-2/close" | line
curl -s "$API/api/branch" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  当前：占用 {d['occupied_amps']}A / 余量 {d['remaining_amps']}A（srv-4 峰值仍保留）\")"
sleep 4
curl -s "$API/api/orders/$OID4B" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  3s 后 srv-4：state={d['state']}，峰值 {d['occupied_amps']}A 仍保留\")"

step "14. 最终支路状态"
curl -s "$API/api/branch" | pretty

echo
echo "演示完成。全部合闸单：GET $API/api/orders ；接口文档：$API/docs"
