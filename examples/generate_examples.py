"""生成一套自洽的示例请求文件（仅使用标准库）。

产出到 examples/generated/：
  batch-1.json .. batch-3.json —— 哈希链连续的三个批次
  batch-3-conflict.json       —— 与 batch-3 同序号但内容不同（触发 409）
  batch-4-premature.json      —— 在缺口存在时提前到达的 #4
  requests.http               —— REST Client 可直接执行的请求集合

用法:
    python3 examples/generate_examples.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.hashing import GENESIS_HASH, batch_hash  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated")
SOURCE_ID = "sensor-shanghai-01"


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    prev = GENESIS_HASH
    batches = []
    for no in range(1, 4):
        events = [
            {
                "event_id": f"evt-{no}-001",
                "ts": f"2026-09-23T0{no}:00:00Z",
                "metric": "temperature",
                "value": 23.0 + no,
                "unit": "C",
            },
            {
                "event_id": f"evt-{no}-002",
                "ts": f"2026-09-23T0{no}:05:00Z",
                "metric": "humidity",
                "value": 55 + no,
                "unit": "%",
            },
        ]
        b_hash = batch_hash(SOURCE_ID, no, prev, events)
        payload = {
            "source_id": SOURCE_ID,
            "batch_no": no,
            "prev_hash": prev,
            "events": events,
        }
        batches.append((no, prev, b_hash, payload))
        prev = b_hash

    for no, _prev_h, _h, payload in batches:
        with open(os.path.join(OUT_DIR, f"batch-{no}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    # 同序号不同内容 -> duplicate_conflict
    conflict = json.loads(json.dumps(batches[2][3]))
    conflict["events"][0]["value"] = 99.9
    with open(os.path.join(OUT_DIR, "batch-3-conflict.json"), "w", encoding="utf-8") as f:
        json.dump(conflict, f, ensure_ascii=False, indent=2)

    # 提前到达的 #4（模拟乱序；缺 #2 时仍可暂存，链闭合前不会封存）
    h3 = batches[2][2]
    premature = {
        "source_id": SOURCE_ID,
        "batch_no": 4,
        "prev_hash": h3,
        "events": [{"event_id": "evt-4-001", "note": "arrived early"}],
    }
    with open(os.path.join(OUT_DIR, "batch-4-premature.json"), "w", encoding="utf-8") as f:
        json.dump(premature, f, ensure_ascii=False, indent=2)

    http = """### 健康检查
GET http://localhost:8000/health

### 创世锚点 sha256("")
GET http://localhost:8000/api/v1/genesis

### 乱序演示：先到 #3（暂存，产生缺口）
POST http://localhost:8000/api/v1/batches
Content-Type: application/json

< ./batch-3.json

### 重试 #3（幂等，HTTP 200, duplicate=true）
POST http://localhost:8000/api/v1/batches
Content-Type: application/json

< ./batch-3.json

### 冲突批次（同 batch_no 但内容不同 -> 409 duplicate_conflict）
POST http://localhost:8000/api/v1/batches
Content-Type: application/json

< ./batch-3-conflict.json

> {% client.test("expect 409", () => client.response.status === 409); %}

### 查缺口（缺 1、2）
GET http://localhost:8000/api/v1/sources/__SID__/gaps

### 链未闭合时尝试封存（-> 409 chain_not_complete）
POST http://localhost:8000/api/v1/seals
Content-Type: application/json

{"source_id": "__SID__"}

### 补 #1、#2
POST http://localhost:8000/api/v1/batches
Content-Type: application/json

< ./batch-1.json

###
POST http://localhost:8000/api/v1/batches
Content-Type: application/json

< ./batch-2.json

### 链闭合，封存，返回确定性 Merkle 根
POST http://localhost:8000/api/v1/seals
Content-Type: application/json

{"source_id": "__SID__"}

### 取 #1 第 0 个事件的包含证明
GET http://localhost:8000/api/v1/batches/__SID__/1/proof?event_index=0

### 查封存状态
GET http://localhost:8000/api/v1/sources/__SID__/seal

### 全量来源视图
GET http://localhost:8000/api/v1/sources
""".replace("__SID__", SOURCE_ID)
    with open(os.path.join(OUT_DIR, "requests.http"), "w", encoding="utf-8") as f:
        f.write(http)

    print(f"示例文件已生成于 {OUT_DIR}")
    for name in sorted(os.listdir(OUT_DIR)):
        print(" -", name)


if __name__ == "__main__":
    main()
