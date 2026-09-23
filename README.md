# 证据批次封存 API（Evidence Batch Sealing）

多个采集端向本服务上传带有 `source_id`、`batch_no`、`prev_hash` 的 JSON 事件批次。
服务端按**来源**独立维护哈希链：

- **乱序接收**：批次可任意顺序到达，未到齐的序号进入暂存区并可查询缺口；
- **重试幂等**：同一 `(source_id, batch_no)` 内容完全一致时安全重试（返回 `200 + duplicate=true`）；
- **冲突明确报错**：同序号不同内容、`prev_hash` 与前驱不符、补洞批次与已到后继声明不符，一律 `409` 并给出错误码与详情；
- **闭合才封存**：只有哈希链从创世批次起连续闭合才能封存，封存生成**确定性双层 Merkle 根**；
- **可校验证明**：封存后可为任一事件出具“事件 → 批次 → 封存根”的包含证明，并提供纯密码学复核接口；
- **来源事件契约（可选）**：可按 `source_id` 注册不可变版本的 JSON Schema 子集
  （`type`/`required`/`properties`/`items`/`enum`），原子切换生效版本；
  提交批次时携带 `contract_version` 即按契约**逐事件**校验，任一失败**整批拒绝**
  并返回逐事件 JSONPath 错误；不带契约的旧采集端行为完全不变。

仅依赖 **FastAPI + SQLite（标准库 sqlite3）**，数据落本地卷，**无需任何外部账号 / 外部服务**。

---

## 目录结构

```
app/
  main.py       # FastAPI 路由、生命周期、统一错误格式
  service.py    # 契约/提交/缺口/封存/证明/校验 业务逻辑
  db.py         # SQLite 连接、建表、串行写锁
  contracts.py  # 受限 JSON Schema 子集：结构校验 + 逐事件 JSONPath 校验
  hashing.py    # 规范化 JSON、SHA-256、Merkle 树/证明
  schemas.py    # 请求模型
  errors.py     # 统一业务错误
examples/
  generate_examples.py  # 生成自洽链的示例批次（仅标准库）
  demo.sh               # curl 端到端演示
  generated/            # 运行生成器后出现（含 requests.http）
tests/                  # pytest 端到端测试（33 项：链 14 + 契约 19）
Dockerfile
docker-compose.yml
```

---

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
# API:        http://localhost:8000
# Swagger UI: http://localhost:8000/docs
# 健康检查:    http://localhost:8000/health
```

SQLite 数据保存在命名卷 `evidence-data`（容器内 `/data/evidence.db`）。
停止后数据仍在；清空数据：`docker compose down -v`。

### 方式二：本地直接运行（Python 3.11+）

```bash
pip install -r requirements.txt
export EVIDENCE_DB=./data/evidence.db   # 可选，默认 /data/evidence.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 一键演示

服务启动后：

```bash
./examples/demo.sh                 # 默认 http://localhost:8000
BASE=http://127.0.0.1:8000 ./examples/demo.sh
```

脚本依次演示：乱序暂存 `#3` → 幂等重试 → 冲突 `409` → 查询缺口 → 未闭合封存 `409`
→ 补齐 `#1/#2` → 封存成功 → 取包含证明 → `/verify` 复核通过。

也可用 VS Code / JetBrains 的 REST Client 打开
`examples/generated/requests.http` 逐个发送（先运行生成器）：

```bash
python3 examples/generate_examples.py
```

### 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

---

## 哈希与封存方案

全部哈希为 **SHA-256（hex）**，并以域分隔前缀防止不同结构之间碰撞：

| 对象 | 计算方式 |
|---|---|
| 创世锚点 | `genesis = sha256("")`，`batch_no=1` 的 `prev_hash` 必须等于它 |
| 事件叶子 | `H(0x00 "EVT" ‖ canonical_json(event))` |
| 批次事件根 | 由各事件叶子构成的 Merkle 树根 `events_root` |
| 批次哈希 | `batch_hash = H(0x00 "BATCH" ‖ canonical_json({source_id, batch_no, prev_hash, events_root}))` |
| 链指针 | 第 n 批的 `prev_hash` 必须等于第 n−1 批的 `batch_hash` |
| 封存批次树根 | 按 `batch_no` 顺序排列各 `batch_hash` 构成的 Merkle 树根 `batch_tree_root` |
| 封存根 | `seal_root = H(0x00 "SEAL" ‖ canonical_json({source_id, from, to, tree, batch_tree_root}))` |

要点：

- **规范化 JSON**：`sort_keys`、无多余空白、UTF-8、不转义非 ASCII —— 同一事件无论
  采集端如何序列化，哈希一致，因此 Merkle 根是**确定性**的；
- Merkle 树为二叉、奇数层复制最后一个节点；内部节点带 `0x01 "NODE"` 前缀且**左右顺序敏感**；
- `seal_root` 额外绑定 `source_id` 与封存区间，防止跨来源、跨区间重放；
- 封存区间始终从 `1` 开始；增量封存时 `batch_tree_root` 覆盖 `1..to_batch_no`
  的**全部**批次（含此前已封存部分），旧区间的证明在密码学上仍自洽，
  但 `/verify` 的 `anchor_matches_server` 会提示其不再是最新锚点。

### 证明结构（两层包含）

`GET .../proof?event_index=i` 返回：

1. `event_leaf` + `event_proof` → 复算到批次的 `events_root`；
2. 用 `prev_hash`、`events_root` 等元数据重算 `batch_hash`，再用 `batch_proof`
   → 复算到封存批次树 `batch_tree_root`；
3. 用 `(source_id, 区间, batch_tree_root)` 重算并比对 `seal_root`。

`POST /api/v1/verify` 仅依赖证明体本身完成上述全部复核（`check_server_anchor=false`
时完全不查库），可离线独立验证。

---

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/v1/genesis` | 返回创世锚点 `sha256("")` |
| POST | `/api/v1/contracts` | 注册（或幂等重试）某来源的契约版本 |
| GET | `/api/v1/contracts` | 列出所有来源的契约版本与生效版本 |
| GET | `/api/v1/contracts/{source_id}` | 列出某来源的全部契约版本 |
| GET | `/api/v1/contracts/{source_id}/{version}` | 获取某契约版本的完整 schema |
| PUT | `/api/v1/contracts/{source_id}/activate` | 原子切换当前生效契约版本 |
| POST | `/api/v1/batches` | 提交/重试一个批次（乱序允许，可带 `contract_version`） |
| GET | `/api/v1/sources` | 所有来源的封存水位与缺口 |
| GET | `/api/v1/sources/{source_id}/gaps` | 查询暂存缺口 |
| GET | `/api/v1/sources/{source_id}/seal` | 查询封存状态与封存根 |
| GET | `/api/v1/batches/{source_id}/{batch_no}` | 批次元数据 + 事件原文 |
| POST | `/api/v1/seals` | 封存连续闭合链 |
| GET | `/api/v1/batches/{source_id}/{batch_no}/proof?event_index=i` | 事件包含证明（须已封存） |
| POST | `/api/v1/verify` | 复核证明（可离线） |

### 1) 提交批次

`POST /api/v1/batches`

```json
{
  "source_id": "sensor-shanghai-01",
  "batch_no": 1,
  "prev_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "events": [
    {"event_id": "evt-1-001", "metric": "temperature", "value": 24.0}
  ]
}
```

- 新批次：`201`；完全相同的重试：`200` 且响应 `duplicate=true`；
- `events` 为非空数组，元素可为任意 JSON；
- 成功响应含 `batch_hash`、`events_root` 与 `next_expected_batch_no`（下一个缺口序号）。

### 1b) 注册 / 激活来源事件契约

契约是**可选**的；没有契约的来源沿用上面的自由 JSON 提交流程，完全向后兼容。

`POST /api/v1/contracts` 为某个 `source_id` 注册一个**不可变版本**的
JSON Schema 子集（仅允许 `type` / `required` / `properties` / `items` / `enum`
五个关键字，其余关键字会被拒绝；子模式递归支持同一子集）：

```json
{
  "source_id": "sensor-shanghai-01",
  "version": "v1",
  "schema": {
    "type": "object",
    "required": ["event_id", "level"],
    "properties": {
      "event_id": {"type": "string"},
      "level": {"type": "string", "enum": ["info", "warn", "error"]},
      "tags": {"type": "array", "items": {"type": "string"}},
      "meta": {"type": "object", "properties": {"temp": {"type": "number"}}}
    }
  }
}
```

- 首次注册：`201`；**同一版本、同一内容**重复注册：`200 + idempotent=true`
  （内容按规范化 JSON 的 `schema_hash` 比较，字段书写顺序不同也判定为相同）；
- 同一版本提交**不同内容**：`409 contract_conflict`——版本一经注册不可修改，
  请注册新版本号（如 `v2`）；
- schema 结构不合法（未知类型、不支持的关键字、空 `required`/`enum` 等）：
  `422 contract_schema_invalid`，`details.violations` 一次返回全部结构问题
  （含 `schema_path`，如 `/properties/level/enum`）。

`PUT /api/v1/contracts/{source_id}/activate` 原子切换生效版本：

```json
{"version": "v1"}
```

```json
{
  "idempotent": false,
  "source_id": "sensor-shanghai-01",
  "active_version": "v1",
  "schema_hash": "59f7598f…",
  "activated_at": "2026-09-23T06:35:02+00:00"
}
```

重复激活同一版本返回 `idempotent=true`；激活未注册版本返回 `404`。
切换是单行 upsert（SQLite 单事务 + 进程写锁），对并发提交是原子的：
每个批次要么整体按旧版本、要么整体按新版本校验。

查询：

- `GET /api/v1/contracts`：所有来源及其版本列表、当前生效版本；
- `GET /api/v1/contracts/{source_id}`：某来源全部版本（含每个版本的
  `schema_hash`、`active`、`created_at`，不含完整 schema）；
- `GET /api/v1/contracts/{source_id}/{version}`：某版本完整 schema 与生效状态。

### 1c) 带契约版本提交批次

提交批次时增加可选字段 `contract_version`：

```json
{
  "source_id": "sensor-shanghai-01",
  "batch_no": 1,
  "prev_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "contract_version": "v1",
  "events": [
    {"event_id": "evt-1", "level": "info", "tags": ["a"], "meta": {"temp": 24.0}}
  ]
}
```

- `contract_version` **必须等于该来源当前生效版本**：未注册 → `404 not_found`，
  已注册但非生效版本 → `409 contract_version_inactive`（响应附带当前
  `active_version`）。典型发布流程：注册新版本 → 激活 → 采集端切换；
- 服务端按该版本 schema **逐事件**校验，**收集全部违规**；任一事件失败则
  **整批拒绝（422，批次不落库）**，哈希链流程完全不启动。错误形如：

```json
{"error": {"code": "contract_violation",
           "message": "批次中有 3 处不符合契约 sensor-shanghai-01@v1，整批拒绝",
           "details": {"source_id": "sensor-shanghai-01",
                       "contract_version": "v1",
                       "violations": [
             {"event_index": 0, "json_path": "$.level",
              "schema_path": "/properties/level/enum",
              "message": "值 'fatal' 不在枚举 ['info', 'warn', 'error'] 中"},
             {"event_index": 0, "json_path": "$.tags[1]",
              "schema_path": "/properties/tags/items/type",
              "message": "类型应为 string，实际为 integer"},
             {"event_index": 2, "json_path": "$.event_id",
              "schema_path": "/required", "message": "缺少必填属性 'event_id'"}]}}}
```

  `json_path` 以单个事件为根：`$` 为事件根，`$.a.b` 取属性，`$[0]` 取数组元素。
- **不带** `contract_version` 的提交不做任何契约校验——即使该来源已有生效契约；
  事件仍可为任意 JSON。这保证旧采集端零改动继续工作。
- 契约不参与哈希：批次/事件哈希、封存 Merkle 根、包含证明的计算方式与此前完全一致；
  成功入库的批次在响应与批次详情中额外回带 `contract_version`（旧批次为 `null`）。


### 2) 查询缺口

`GET /api/v1/sources/sensor-shanghai-01/gaps`

```json
{
  "source_id": "sensor-shanghai-01",
  "sealed_to": 0,
  "next_expected_batch_no": 1,
  "staged_batch_nos": [3],
  "missing_batch_nos": [1, 2],
  "chain_complete": false
}
```

### 3) 封存

`POST /api/v1/seals`，body `{"source_id": "..."}` 封存整条已闭合的暂存链；
也可 `{"source_id": "...", "to_batch_no": 2}` 只封存连续前缀（后续批次仍在暂存区）。

```json
{
  "idempotent": false,
  "source_id": "sensor-shanghai-01",
  "from_batch_no": 1,
  "to_batch_no": 3,
  "batch_tree_root": "219127ce…2daf",
  "seal_root": "09dfc228…5d64",
  "sealed_batch_nos": [1, 2, 3],
  "sealed_at": "2026-09-23T01:27:06+00:00"
}
```

封存时会从库中重新逐批复算链指针与批次哈希；库内数据被篡改会得到
`409 tampered_batch`。对同一水位重复封存返回 `idempotent=true` 的同一条记录。

### 4) 取证明 / 校验

- `GET /api/v1/batches/{sid}/1/proof?event_index=0`
- `POST /api/v1/verify`：把上一个响应的 `proof` 各字段原样回传（字段名见
  `app/schemas.py:VerifyRequest`），返回各项布尔结论；篡改任一事件字段都会使
  `event_inclusion_valid=false`、`valid=false`。

---

## 错误格式与错误码

所有业务错误统一为：

```json
{"error": {"code": "chain_not_complete",
           "message": "链未闭合，无法封存：…",
           "details": {"missing_batch_nos": [1, 2]}}}
```

| HTTP | code | 触发场景 |
|---|---|---|
| 409 | `bad_genesis_anchor` | `batch_no=1` 的 `prev_hash` 不是 `sha256("")` |
| 409 | `prev_hash_mismatch` | 与已存在前驱的 `batch_hash` 不一致 |
| 409 | `chain_conflict` | 补洞批次的哈希不等于已到后继声明的 `prev_hash` |
| 409 | `duplicate_conflict` | 同序号重试但内容/`prev_hash` 不一致 |
| 409 | `chain_not_complete` | 有缺口时封存（响应 `details.missing_batch_nos`） |
| 409 | `broken_chain` | 封存复算时发现链指针断裂（纵深防御） |
| 409 | `tampered_batch` | 库内事件与其存储的 `batch_hash` 对不上 |
| 409 | `not_sealed` | 未封存批次请求出具证明 |
| 409 | `batch_sealed` | 试图改动已封存批次 |
| 409 | `contract_conflict` | 同 `(source_id, version)` 注册了不同 schema（版本不可变） |
| 409 | `contract_version_inactive` | 批次携带的版本不是该来源当前生效版本（details 附 `active_version`） |
| 422 | `contract_schema_invalid` | 注册的 schema 不属于受支持子集（details.violations 含 `schema_path`） |
| 422 | `contract_violation` | 批次事件不符契约，整批拒绝（details.violations 含 `event_index`/`json_path`） |
| 404 | `not_found` | 批次/契约版本/资源不存在 |
| 422 | `validation_error` / 请求校验 | 空事件、非法 `batch_no`、非法 `prev_hash` 等 |

---

## 并发与持久化说明

- SQLite 开启 **WAL** 与 `busy_timeout`；所有“读-判断-写”流程在进程级互斥锁 +
  单事务内完成，多个采集端并发提交/封存/切换契约不会产生半截链判断
  （契约版本激活为单行 upsert，批次校验在同一把写锁内读到一致的生效版本）；
- 契约版本一经注册不可变，仅 `active_contracts` 指针可切换；旧版本记录永久保留
  以供历史批次追溯（批次行存 `contract_version`）；
- 旧版本数据库启动时自动迁移（为 `batches` 补 `contract_version` 列），无需手工处理；
- 单实例部署即满足典型采集场景。多实例共享同一数据库文件不在本方案目标内
  （SQLite 写串行，且进程锁不跨机）；如需横向扩展，可把 `db.py` 替换为
  Postgres 等外部存储，业务层接口不变；
- 失败请求统一 `rollback()`，避免连接复用期间残留未提交事务。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `EVIDENCE_DB` | `/data/evidence.db` | SQLite 文件路径（父目录自动创建） |

容器以非 root 用户（uid 10001）运行。
