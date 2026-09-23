#!/usr/bin/env bash
# 端到端演示：乱序提交 -> 幂等/冲突 -> 缺口 -> 闭合 -> 封存 -> 出证 -> 校验
# 依赖：bash、curl、python3（标准库）。无需任何外部账号。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
SID="sensor-shanghai-01"
DIR="$(cd "$(dirname "$0")" && pwd)"
GEN="$DIR/generated"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少依赖: $1" >&2; exit 1; }; }
need curl
need python3

echo "==> 生成自洽示例批次"
python3 "$DIR/generate_examples.py" >/dev/null

echo "==> 健康检查"
curl -fsS "$BASE/health"; echo

echo "==> [乱序] 先提交 #3（暂存，#1/#2 为缺口）"
curl -fsS -X POST "$BASE/api/v1/batches" -H 'Content-Type: application/json' \
    -d @"$GEN/batch-3.json" | python3 -m json.tool

echo "==> [幂等重试] 再次提交完全相同的 #3（HTTP 200, duplicate=true）"
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/api/v1/batches" \
    -H 'Content-Type: application/json' -d @"$GEN/batch-3.json"

echo "==> [冲突] 提交同序号不同内容的 #3（期望 409）"
curl -sS -o /tmp/conflict.json -w 'HTTP %{http_code}\n' -X POST "$BASE/api/v1/batches" \
    -H 'Content-Type: application/json' -d @"$GEN/batch-3-conflict.json"
python3 -m json.tool /tmp/conflict.json

echo "==> 查询缺口"
curl -fsS "$BASE/api/v1/sources/$SID/gaps" | python3 -m json.tool

echo "==> [失败] 链未闭合尝试封存（期望 409）"
curl -sS -X POST "$BASE/api/v1/seals" -H 'Content-Type: application/json' \
    -d "{\"source_id\":\"$SID\"}" -w '\nHTTP %{http_code}\n'

echo "==> 补齐 #1 与 #2"
curl -fsS -o /dev/null -X POST "$BASE/api/v1/batches" -H 'Content-Type: application/json' \
    -d @"$GEN/batch-1.json"
curl -fsS -o /dev/null -X POST "$BASE/api/v1/batches" -H 'Content-Type: application/json' \
    -d @"$GEN/batch-2.json"

echo "==> 链闭合，封存，返回确定性 Merkle 根"
curl -fsS -X POST "$BASE/api/v1/seals" -H 'Content-Type: application/json' \
    -d "{\"source_id\":\"$SID\"}" | tee /tmp/seal.json | python3 -m json.tool

echo "==> 获取 #1 第 0 个事件的包含证明"
curl -fsS "$BASE/api/v1/batches/$SID/1/proof?event_index=0" > /tmp/proof.json
python3 -m json.tool /tmp/proof.json

echo "==> 用 /verify 做纯密码学复核（并比对服务端锚点）"
python3 - "$BASE" <<'PY'
import json, sys, urllib.request

base = sys.argv[1]
p = json.load(open("/tmp/proof.json"))
body = {
    "source_id": p["source_id"],
    "batch_no": p["batch_no"],
    "prev_hash": p["batch"]["prev_hash"],
    "event": p["event"],
    "event_index": p["event_index"],
    "event_proof": p["event_proof"],
    "events_root": p["events_root"],
    "batch_proof": p["batch"]["batch_proof"],
    "seal_from": p["seal"]["from_batch_no"],
    "seal_to": p["seal"]["to_batch_no"],
    "batch_tree_root": p["seal"]["batch_tree_root"],
    "seal_root": p["seal"]["seal_root"],
    "check_server_anchor": True,
}
req = urllib.request.Request(
    base + "/api/v1/verify",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req).read().decode())
PY

echo
echo "演示完成。交互式请求见: $GEN/requests.http"
echo "接口文档:   $BASE/docs"
