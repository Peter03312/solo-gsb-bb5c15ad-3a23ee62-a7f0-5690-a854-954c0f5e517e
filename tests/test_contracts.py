"""端到端行为测试：契约注册/幂等/冲突、激活切换、按契约校验批次、查询与兼容性。"""
import hashlib

from app.hashing import GENESIS_HASH, batch_hash


def _events(n, tag="e"):
    return [{"seq": i, "tag": tag, "payload": f"data-{i}"} for i in range(n)]


EVENT_SCHEMA = {
    "type": "object",
    "required": ["event_id", "level"],
    "properties": {
        "event_id": {"type": "string"},
        "level": {"type": "string", "enum": ["info", "warn", "error"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "meta": {"type": "object", "properties": {"temp": {"type": "number"}}},
    },
}


def _valid_events():
    return [
        {"event_id": "evt-1", "level": "info", "tags": ["a"], "meta": {"temp": 24}},
        {"event_id": "evt-2", "level": "error", "tags": [], "meta": {"temp": 0.5}},
    ]


def _register(client, sid, version, schema=None, status=None):
    r = client.post(
        "/api/v1/contracts",
        json={"source_id": sid, "version": version, "schema": schema or EVENT_SCHEMA},
    )
    if status is not None:
        assert r.status_code == status, r.text
    return r


def _activate(client, sid, version, status=None):
    r = client.put(
        f"/api/v1/contracts/{sid}/activate", json={"version": version}
    )
    if status is not None:
        assert r.status_code == status, r.text
    return r


def _submit(client, source, no, prev, events, status=None, contract_version=None):
    body = {
        "source_id": source,
        "batch_no": no,
        "prev_hash": prev,
        "events": events,
    }
    if contract_version is not None:
        body["contract_version"] = contract_version
    r = client.post("/api/v1/batches", json=body)
    if status is not None:
        assert r.status_code == status, r.text
    return r


# ---------------------------------------------------------------- 注册

def test_register_contract_then_get(client):
    sid = "src-c1"
    r = _register(client, sid, "v1", status=201)
    body = r.json()
    assert body["idempotent"] is False
    assert body["contract"]["source_id"] == sid
    assert body["contract"]["version"] == "v1"
    assert body["contract"]["schema"] == EVENT_SCHEMA
    assert len(body["contract"]["schema_hash"]) == 64

    # 单版本查询
    r = client.get(f"/api/v1/contracts/{sid}/v1")
    assert r.status_code == 200
    detail = r.json()
    assert detail["schema"] == EVENT_SCHEMA
    assert detail["active"] is False


def test_register_same_version_same_schema_is_idempotent(client):
    sid = "src-c2"
    r1 = _register(client, sid, "v1", status=201)
    # 字段顺序不同的等价 JSON 也视为相同内容（规范化哈希）
    reordered = {
        "properties": EVENT_SCHEMA["properties"],
        "type": "object",
        "required": EVENT_SCHEMA["required"],
    }
    r2 = _register(client, sid, "v1", schema=reordered, status=200)
    assert r2.json()["idempotent"] is True
    assert r2.json()["contract"]["schema_hash"] == r1.json()["contract"]["schema_hash"]


def test_register_same_version_different_schema_conflicts(client):
    sid = "src-c3"
    _register(client, sid, "v1", status=201)
    changed = dict(EVENT_SCHEMA)
    changed["required"] = ["event_id"]  # 内容变化
    r = _register(client, sid, "v1", schema=changed)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "contract_conflict"
    assert err["details"]["version"] == "v1"
    assert err["details"]["existing_schema_hash"] != err["details"]["submitted_schema_hash"]


def test_register_invalid_schema_rejected(client):
    sid = "src-c4"
    # 不支持的关键字
    r = _register(client, sid, "v1", schema={"type": "object", "minimum": 1})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "contract_schema_invalid"
    assert any("minimum" in v["message"] for v in err["details"]["violations"])

    # 未知类型 + 空 enum，收集全部结构问题
    r = _register(client, sid, "v2", schema={"type": "date", "enum": []})
    assert r.status_code == 422
    messages = [v["message"] for v in r.json()["error"]["details"]["violations"]]
    assert any("date" in m for m in messages)
    assert any("enum" in m for m in messages)


# ---------------------------------------------------------------- 激活

def test_activate_and_idempotent(client):
    sid = "src-c5"
    _register(client, sid, "v1", status=201)
    r = _activate(client, sid, "v1", status=200)
    assert r.json()["active_version"] == "v1"
    assert r.json()["idempotent"] is False

    # 重复激活同一版本 -> 幂等，activated_at 不变
    r2 = _activate(client, sid, "v1", status=200)
    assert r2.json()["idempotent"] is True
    assert r2.json()["activated_at"] == r.json()["activated_at"]

    detail = client.get(f"/api/v1/contracts/{sid}/v1").json()
    assert detail["active"] is True


def test_activate_unregistered_version_404(client):
    r = _activate(client, "src-c6", "nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_activate_atomically_switches_active_version(client):
    sid = "src-c7"
    v2_schema = {
        "type": "object",
        "required": ["event_id", "level", "trace_id"],
        "properties": {"event_id": {"type": "string"}, "level": {"type": "string"},
                       "trace_id": {"type": "string"}},
    }
    _register(client, sid, "v1", status=201)
    _register(client, sid, "v2", schema=v2_schema, status=201)
    _activate(client, sid, "v1", status=200)

    listing = client.get(f"/api/v1/contracts/{sid}").json()
    assert listing["active_version"] == "v1"
    assert {v["version"] for v in listing["versions"]} == {"v1", "v2"}
    assert {v["active"] for v in listing["versions"]} == {True, False}

    # 原子切换
    r = _activate(client, sid, "v2", status=200)
    assert r.json()["active_version"] == "v2"
    assert r.json()["idempotent"] is False
    assert client.get(f"/api/v1/contracts/{sid}").json()["active_version"] == "v2"
    assert client.get(f"/api/v1/contracts/{sid}/v1").json()["active"] is False
    assert client.get(f"/api/v1/contracts/{sid}/v2").json()["active"] is True


# ---------------------------------------------------------------- 批次校验

def test_submit_with_contract_version_validates_and_passes(client):
    sid = "src-c8"
    _register(client, sid, "v1", status=201)
    _activate(client, sid, "v1", status=200)
    r = _submit(client, sid, 1, GENESIS_HASH, _valid_events(), 201, "v1")
    body = r.json()
    assert body["batch"]["contract_version"] == "v1"

    # 批次详情回带 contract_version
    detail = client.get(f"/api/v1/batches/{sid}/1").json()
    assert detail["contract_version"] == "v1"


def test_submit_contract_violation_rejects_whole_batch_with_jsonpath(client):
    sid = "src-c9"
    _register(client, sid, "v1", status=201)
    _activate(client, sid, "v1", status=200)

    bad_events = [
        _valid_events()[0],
        {
            "event_id": 123,                          # 类型错误
            "level": "fatal",                         # 枚举错误
            "tags": ["ok", 7],                        # 数组元素类型错误
            "meta": {"temp": "hot"},                  # 嵌套类型错误
        },
        {"level": "info"},                            # 缺必填 event_id
    ]
    r = _submit(client, sid, 1, GENESIS_HASH, bad_events)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "contract_violation"
    assert err["details"]["contract_version"] == "v1"
    violations = err["details"]["violations"]

    paths = {(v["event_index"], v["json_path"]) for v in violations}
    assert (1, "$.event_id") in paths
    assert (1, "$.level") in paths
    assert (1, "$.tags[1]") in paths
    assert (1, "$.meta.temp") in paths
    assert (2, "$.event_id") in paths
    assert all(v["schema_path"].startswith("/") for v in violations)

    # 整批拒绝：批次未落库，暂存区为空
    gaps = client.get(f"/api/v1/sources/{sid}/gaps").json()
    assert gaps["staged_batch_nos"] == []
    assert gaps["missing_batch_nos"] == []


def test_failed_contract_batch_does_not_block_later_valid_submit(client):
    sid = "src-c10"
    _register(client, sid, "v1", status=201)
    _activate(client, sid, "v1", status=200)

    bad = [{"event_id": 1, "level": "info"}]
    _submit(client, sid, 1, GENESIS_HASH, bad, 422, "v1")

    r = _submit(client, sid, 1, GENESIS_HASH, _valid_events(), 201, "v1")
    assert r.json()["duplicate"] is False


def test_submit_unregistered_contract_version_404(client):
    r = _submit(client, "src-c11", 1, GENESIS_HASH, _valid_events(), 404, "ghost")
    assert r.json()["error"]["code"] == "not_found"


def test_submit_inactive_contract_version_409(client):
    sid = "src-c12"
    _register(client, sid, "v1", status=201)
    _register(client, sid, "v2", status=201)
    _activate(client, sid, "v2", status=200)

    r = _submit(client, sid, 1, GENESIS_HASH, _valid_events(), 409, "v1")
    err = r.json()["error"]
    assert err["code"] == "contract_version_inactive"
    assert err["details"]["requested_version"] == "v1"
    assert err["details"]["active_version"] == "v2"
    # 未落库
    assert client.get(f"/api/v1/sources/{sid}/gaps").json()["staged_batch_nos"] == []


def test_submit_without_contract_version_stays_compatible(client):
    sid = "src-c13"
    _register(client, sid, "v1", status=201)
    _activate(client, sid, "v1", status=200)

    # 即使存在生效契约，不带 contract_version 也完全不校验（向后兼容）
    freeform = [{"anything": [1, 2, 3]}, 42, "raw"]
    r = _submit(client, sid, 1, GENESIS_HASH, freeform, 201)
    assert r.json()["batch"]["contract_version"] is None


def test_contract_version_does_not_change_batch_hash(client):
    """成功批次的哈希流程与契约无关：相同事件哈希一致。"""
    sid_a, sid_b = "src-c14a", "src-c14b"
    events = _events(2)
    expected = batch_hash(sid_a, 1, GENESIS_HASH, events)

    _register(client, sid_a, "v1", status=201)
    _activate(client, sid_a, "v1", status=200)
    r1 = _submit(client, sid_a, 1, GENESIS_HASH, events, 201, "v1")
    r2 = _submit(client, sid_b, 1, GENESIS_HASH, events, 201)
    assert r1.json()["batch"]["batch_hash"] == expected
    assert r1.json()["batch"]["batch_hash"] == r2.json()["batch"]["batch_hash"]


def test_duplicate_retry_with_contract_version_is_idempotent(client):
    sid = "src-c15"
    _register(client, sid, "v1", status=201)
    _activate(client, sid, "v1", status=200)
    r1 = _submit(client, sid, 1, GENESIS_HASH, _valid_events(), 201, "v1")
    r2 = _submit(client, sid, 1, GENESIS_HASH, _valid_events(), 200, "v1")
    assert r2.json()["duplicate"] is True
    assert r2.json()["batch"]["batch_hash"] == r1.json()["batch"]["batch_hash"]


def test_contract_validation_runs_before_chain_conflict(client):
    """契约失败优先于链冲突暴露：违规批次不会进入链判定。"""
    sid = "src-c16"
    _register(client, sid, "v1", status=201)
    _activate(client, sid, "v1", status=200)
    bad = [{"event_id": 1, "level": "info"}]
    r = _submit(client, sid, 1, GENESIS_HASH, bad, 422, "v1")
    assert r.json()["error"]["code"] == "contract_violation"


# ---------------------------------------------------------------- 全流程 + 查询

def test_contract_batch_seals_and_proves_normally(client):
    sid = "src-c17"
    _register(client, sid, "v1", status=201)
    _activate(client, sid, "v1", status=200)

    prev = GENESIS_HASH
    for no in (1, 2):
        evs = [
            {"event_id": f"evt-{no}-{i}", "level": "info"}
            for i in range(2)
        ]
        r = _submit(client, sid, no, prev, evs, 201, "v1")
        prev = r.json()["batch"]["batch_hash"]

    seal = client.post("/api/v1/seals", json={"source_id": sid}).json()
    assert seal["to_batch_no"] == 2

    proof = client.get(f"/api/v1/batches/{sid}/1/proof?event_index=0").json()
    r = client.post("/api/v1/verify", json={
        "source_id": sid,
        "batch_no": proof["batch_no"],
        "prev_hash": proof["batch"]["prev_hash"],
        "event": proof["event"],
        "event_index": proof["event_index"],
        "event_proof": proof["event_proof"],
        "events_root": proof["events_root"],
        "batch_proof": proof["batch"]["batch_proof"],
        "seal_from": proof["seal"]["from_batch_no"],
        "seal_to": proof["seal"]["to_batch_no"],
        "batch_tree_root": proof["seal"]["batch_tree_root"],
        "seal_root": proof["seal"]["seal_root"],
    })
    assert r.json()["valid"] is True


def test_contract_query_endpoints(client):
    # 无任何契约：全量列表为空，单来源 404
    r = client.get("/api/v1/contracts")
    assert r.status_code == 200
    assert r.json() == {"sources": []}
    assert client.get("/api/v1/contracts/nobody").status_code == 404
    assert client.get("/api/v1/contracts/nobody/v1").status_code == 404

    sid = "src-c18"
    _register(client, sid, "v1", status=201)
    r = client.get("/api/v1/contracts")
    assert r.json()["sources"] == [
        {"source_id": sid, "active_version": None, "versions": ["v1"]}
    ]

    r = client.get(f"/api/v1/contracts/{sid}")
    assert r.status_code == 200
    assert r.json()["active_version"] is None
    assert r.json()["versions"][0]["version"] == "v1"
    assert r.json()["versions"][0]["active"] is False
    assert len(r.json()["versions"][0]["schema_hash"]) == 64


def test_root_level_scalar_and_array_enum_contract(client):
    """契约也允许非对象事件（标量枚举 / 数组 items）。"""
    sid = "src-c19"
    schema = {
        "type": "array",
        "items": {"type": "string", "enum": ["a", "b"]},
    }
    _register(client, sid, "v1", schema=schema, status=201)
    _activate(client, sid, "v1", status=200)

    _submit(client, sid, 1, GENESIS_HASH, [["a", "b"]], 201, "v1")
    r = _submit(client, sid, 2, hashlib.sha256(b"x").hexdigest(), [["a", "c"]], 422, "v1")
    v = r.json()["error"]["details"]["violations"]
    assert (v[0]["event_index"], v[0]["json_path"]) == (0, "$[1]")
