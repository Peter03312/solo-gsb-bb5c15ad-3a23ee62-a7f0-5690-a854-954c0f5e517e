"""来源事件契约端到端测试：注册幂等/冲突、激活、按契约校验、JSONPath、兼容性。"""
from app.hashing import GENESIS_HASH, batch_hash


def _events(n, tag="e"):
    return [{"seq": i, "tag": tag, "payload": f"data-{i}"} for i in range(n)]


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


def _register(client, source, version, schema, status=None):
    r = client.post(
        "/api/v1/contracts",
        json={"source_id": source, "version": version, "schema": schema},
    )
    if status is not None:
        assert r.status_code == status, r.text
    return r


def _activate(client, source, version, status=None):
    r = client.put(
        f"/api/v1/contracts/{source}/activate", json={"version": version}
    )
    if status is not None:
        assert r.status_code == status, r.text
    return r


EVENT_SCHEMA = {
    "type": "object",
    "required": ["event_id", "metric", "value"],
    "properties": {
        "event_id": {"type": "string"},
        "metric": {"type": "string", "enum": ["temperature", "humidity"]},
        "value": {"type": "number"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "meta": {
            "type": "object",
            "properties": {"zone": {"type": "string"}},
        },
    },
}

VALID_EVENTS = [
    {
        "event_id": "evt-1",
        "metric": "temperature",
        "value": 24.0,
        "tags": ["a", "b"],
        "meta": {"zone": "sh"},
    },
    {
        "event_id": "evt-2",
        "metric": "humidity",
        "value": 60,  # integer 对 number 合法
    },
]


# ---------------------------------------------------------------- 注册

def test_register_contract_returns_201(client):
    r = _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    body = r.json()
    assert body["source_id"] == "src-c"
    assert body["version"] == "v1"
    assert body["idempotent"] is False
    assert len(body["content_hash"]) == 64
    assert body["schema"]["required"] == EVENT_SCHEMA["required"]

    # 尚未激活
    r = client.get("/api/v1/contracts/src-c/active")
    assert r.status_code == 404


def test_register_same_version_same_schema_is_idempotent_ignoring_key_order(client):
    r1 = _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    reordered = dict(reversed(list(EVENT_SCHEMA.items())))
    r2 = _register(client, "src-c", "v1", reordered, 200)
    assert r2.json()["idempotent"] is True
    assert r2.json()["content_hash"] == r1.json()["content_hash"]


def test_register_same_version_different_schema_conflicts(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    changed = {**EVENT_SCHEMA, "required": ["event_id", "metric", "value", "extra"]}
    r = _register(client, "src-c", "v1", changed, 409)
    assert r.json()["error"]["code"] == "contract_conflict"
    # 旧 schema 未被覆盖
    stored = client.get("/api/v1/contracts/src-c/v1").json()
    assert stored["schema"]["required"] == EVENT_SCHEMA["required"]


def test_register_rejects_unknown_keyword_and_malformed_schema(client):
    r = _register(client, "src-c", "v1", {"type": "object", "minLength": 1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_contract_schema"

    r = _register(client, "src-c", "v1", {"type": "weird"})
    assert r.status_code == 422

    r = _register(client, "src-c", "v1", {"required": []})
    assert r.status_code == 422

    r = _register(client, "src-c", "v1", {"required": ["ok", "ok"]})
    assert r.status_code == 422

    r = _register(client, "src-c", "v1", {"properties": {"x": {"type": "object", "minimum": 0}}})
    assert r.status_code == 422

    r = _register(client, "src-c", "v1", {"items": {"type": "string", "enum": []}})
    assert r.status_code == 422

    # schema 必须是对象
    r = client.post(
        "/api/v1/contracts",
        json={"source_id": "src-c", "version": "v1", "schema": ["not-object"]},
    )
    assert r.status_code == 422


def test_empty_schema_accepts_anything(client):
    _register(client, "src-c", "v1", {}, 201)
    _activate(client, "src-c", "v1", 200)
    r = _submit(
        client, "src-c", 1, GENESIS_HASH,
        [{"anything": [1, True, None]}, 42, "x", None],
        contract_version="v1",
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- 激活 / 查询

def test_activate_unknown_version_404(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    r = _activate(client, "src-c", "v9", 404)
    assert r.json()["error"]["code"] == "not_found"


def test_activate_switch_is_atomic_and_idempotent(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    schema_v2 = {**EVENT_SCHEMA, "required": ["event_id", "metric", "value", "ts"]}
    _register(client, "src-c", "v2", schema_v2, 201)

    r = _activate(client, "src-c", "v1", 200)
    assert r.json()["version"] == "v1"
    assert r.json()["idempotent"] is False
    assert "activated_at" in r.json()

    r = _activate(client, "src-c", "v1", 200)
    assert r.json()["idempotent"] is True

    r = _activate(client, "src-c", "v2", 200)
    assert r.json()["idempotent"] is False
    assert r.json()["version"] == "v2"

    # 生效指针整体切换：active 与列表标记均指向 v2
    assert client.get("/api/v1/contracts/src-c/active").json()["version"] == "v2"
    versions = client.get("/api/v1/contracts/src-c").json()["versions"]
    assert {v["version"]: v["active"] for v in versions} == {"v1": False, "v2": True}

    all_sources = client.get("/api/v1/contracts").json()["sources"]
    assert [s["source_id"] for s in all_sources] == ["src-c"]
    assert all_sources[0]["active_version"] == "v2"


def test_get_active_before_activation_404(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    r = client.get("/api/v1/contracts/src-c/active")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_contract_query_unknown_source_404(client):
    assert client.get("/api/v1/contracts/nope").status_code == 404
    assert client.get("/api/v1/contracts/nope/v1").status_code == 404
    assert client.get("/api/v1/contracts/nope/active").status_code == 404


# ---------------------------------------------------------------- 按契约校验

def test_batch_with_unregistered_contract_version_404(client):
    r = _submit(
        client, "src-c", 1, GENESIS_HASH, VALID_EVENTS,
        contract_version="ghost",
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_batch_with_registered_but_inactive_version_409(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    r = _submit(
        client, "src-c", 1, GENESIS_HASH, VALID_EVENTS,
        contract_version="v1",
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "contract_not_active"
    assert err["details"]["requested_version"] == "v1"
    assert err["details"]["active_version"] is None


def test_valid_batch_under_active_contract_passes_and_hash_flow_unchanged(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)

    expected_hash = batch_hash("src-c", 1, GENESIS_HASH, VALID_EVENTS)
    r = _submit(
        client, "src-c", 1, GENESIS_HASH, VALID_EVENTS, 201,
        contract_version="v1",
    )
    body = r.json()
    assert body["batch"]["batch_hash"] == expected_hash  # 契约不参与哈希
    assert body["batch"]["contract_version"] == "v1"

    # 批次详情回显契约版本
    detail = client.get("/api/v1/batches/src-c/1").json()
    assert detail["contract_version"] == "v1"


def test_one_bad_event_rejects_entire_batch_with_jsonpath(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)

    bad_events = [
        VALID_EVENTS[0],
        {
            "event_id": "evt-bad",
            "metric": "pressure",   # 不在 enum
            "value": "not-a-number",
            "tags": ["ok", 3],      # 数组第 2 项类型错
            "extra": 1,
        },
    ]
    r = _submit(
        client, "src-c", 1, GENESIS_HASH, bad_events,
        contract_version="v1",
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "contract_violation"
    paths = {v["path"] for v in err["details"]["violations"]}
    assert "$.events[1].metric" in paths           # enum 违规
    assert "$.events[1].value" in paths            # type 违规
    assert "$.events[1].tags[1]" in paths          # items 违规

    by_path = {v["path"]: v for v in err["details"]["violations"]}
    assert by_path["$.events[1].metric"]["rule"] == "enum"
    assert by_path["$.events[1].value"]["rule"] == "type"
    assert by_path["$.events[1].value"]["actual"] == "string"

    # 整批拒绝：批次未入库
    assert client.get("/api/v1/sources/src-c/gaps").json()["staged_batch_nos"] == []


def test_missing_required_field_reported(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)
    r = _submit(
        client, "src-c", 1, GENESIS_HASH,
        [{"event_id": "x", "metric": "temperature"}],
        contract_version="v1",
    )
    assert r.status_code == 422
    paths = {v["path"] for v in r.json()["error"]["details"]["violations"]}
    assert "$.events[0].value" in paths


def test_root_level_type_mismatch_reports_event_root(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)
    r = _submit(
        client, "src-c", 1, GENESIS_HASH, ["just-a-string"],
        contract_version="v1",
    )
    assert r.status_code == 422
    violations = r.json()["error"]["details"]["violations"]
    assert violations[0]["path"] == "$.events[0]"
    assert violations[0]["rule"] == "type"


def test_type_semantics_integer_boolean_null(client):
    schema = {
        "type": "object",
        "required": ["n", "flag"],
        "properties": {
            "n": {"type": "integer"},
            "flag": {"type": "boolean"},
        },
    }
    _register(client, "src-c", "v1", schema, 201)
    _activate(client, "src-c", "v1", 200)

    # 1.0 不是 integer；True 不是 number/integer；null 单独可校验
    r = _submit(
        client, "src-c", 1, GENESIS_HASH, [{"n": 1.0, "flag": 1}],
        contract_version="v1",
    )
    assert r.status_code == 422
    paths = {v["path"] for v in r.json()["error"]["details"]["violations"]}
    assert "$.events[0].n" in paths
    assert "$.events[0].flag" in paths

    r = _submit(
        client, "src-c", 1, GENESIS_HASH, [{"n": 7, "flag": False}],
        contract_version="v1",
    )
    assert r.status_code == 201, r.text


def test_contract_check_runs_for_each_event_and_all_reported(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)
    r = _submit(
        client, "src-c", 1, GENESIS_HASH,
        [42, {"event_id": 1}],  # 两个事件都坏
        contract_version="v1",
    )
    assert r.status_code == 422
    indexes = {v["event_index"] for v in r.json()["error"]["details"]["violations"]}
    assert indexes == {0, 1}


def test_switch_active_version_changes_enforcement(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    schema_v2 = {
        "type": "object",
        "required": ["event_id"],
        "properties": {"event_id": {"type": "string"}},
    }
    _register(client, "src-c", "v2", schema_v2, 201)
    _activate(client, "src-c", "v1", 200)

    # v1 生效时 v2 版本号不可用
    r = _submit(
        client, "src-c", 1, GENESIS_HASH, VALID_EVENTS,
        contract_version="v2",
    )
    assert r.status_code == 409
    assert r.json()["error"]["details"]["active_version"] == "v1"

    _activate(client, "src-c", "v2", 200)
    # v2 更宽松：没有 metric/value 也合法
    r = _submit(
        client, "src-c", 1, GENESIS_HASH, [{"event_id": "only-id"}], 201,
        contract_version="v2",
    )
    # 切换后旧版本立即失效
    r = _submit(
        client, "src-c", 2, r.json()["batch"]["batch_hash"], VALID_EVENTS,
        contract_version="v1",
    )
    assert r.status_code == 409


def test_idempotent_retry_with_contract_version_still_200(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)
    r1 = _submit(
        client, "src-c", 1, GENESIS_HASH, VALID_EVENTS, 201,
        contract_version="v1",
    )
    r2 = _submit(
        client, "src-c", 1, GENESIS_HASH, VALID_EVENTS, 200,
        contract_version="v1",
    )
    assert r2.json()["duplicate"] is True
    assert r2.json()["batch"]["batch_hash"] == r1.json()["batch"]["batch_hash"]


def test_omitting_contract_version_stays_backward_compatible(client):
    # 注册并激活契约后，不带 contract_version 的提交照常工作
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)

    r = _submit(client, "src-c", 1, GENESIS_HASH, _events(2), 201)
    assert r.json()["batch"]["contract_version"] is None

    # 无契约的来源同样不受影响
    r = _submit(client, "src-plain", 1, GENESIS_HASH, _events(1), 201)
    assert r.json()["batch"]["contract_version"] is None
    assert (
        client.get("/api/v1/sources/src-plain/gaps").json()[
            "active_contract_version"
        ]
        is None
    )


def test_gaps_reports_active_contract_version(client):
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)
    gaps = client.get("/api/v1/sources/src-c/gaps").json()
    assert gaps["active_contract_version"] == "v1"


def test_contract_validation_then_seal_and_proof_flow(client):
    """契约校验通过后，封存/证明/复核全流程与既有哈希方案完全一致。"""
    _register(client, "src-c", "v1", EVENT_SCHEMA, 201)
    _activate(client, "src-c", "v1", 200)

    prev = GENESIS_HASH
    for no in (1, 2):
        r = _submit(
            client, "src-c", no, prev, VALID_EVENTS, 201,
            contract_version="v1",
        )
        prev = r.json()["batch"]["batch_hash"]

    seal = client.post("/api/v1/seals", json={"source_id": "src-c"}).json()
    assert seal["to_batch_no"] == 2

    proof = client.get(
        "/api/v1/batches/src-c/1/proof?event_index=0"
    ).json()
    r = client.post("/api/v1/verify", json={
        "source_id": "src-c",
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


def test_invalid_contract_request_body_validation(client):
    # 缺少 version / 空 source_id
    r = client.post(
        "/api/v1/contracts",
        json={"source_id": "s", "schema": {"type": "object"}},
    )
    assert r.status_code == 422

    # activate 缺少 body
    r = client.put("/api/v1/contracts/s/activate")
    assert r.status_code == 422

    # 激活未知版本
    r = client.put(
        "/api/v1/contracts/s/activate", json={"version": "v1"}
    )
    assert r.status_code == 404
