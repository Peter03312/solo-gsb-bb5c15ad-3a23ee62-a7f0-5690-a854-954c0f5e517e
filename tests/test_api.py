"""端到端行为测试：乱序暂存、幂等、冲突、封存、证明与校验。"""
import hashlib

from app.hashing import GENESIS_HASH


def _events(n, tag="e"):
    return [{"seq": i, "tag": tag, "payload": f"data-{i}"} for i in range(n)]


def _submit(client, source, no, prev, events, status=None):
    r = client.post(
        "/api/v1/batches",
        json={
            "source_id": source,
            "batch_no": no,
            "prev_hash": prev,
            "events": events,
        },
    )
    if status is not None:
        assert r.status_code == status, r.text
    return r


def test_genesis_anchor():
    assert GENESIS_HASH == hashlib.sha256(b"").hexdigest()


def test_in_order_submit_seal_proof_verify(client):
    sid = "src-A"
    r = client.get("/api/v1/genesis")
    assert r.json()["genesis_prev_hash"] == GENESIS_HASH

    prev = GENESIS_HASH
    h1 = h2 = None
    for no in (1, 2):
        evs = _events(3, f"b{no}")
        r = _submit(client, sid, no, prev, evs, 201)
        body = r.json()
        assert body["duplicate"] is False
        assert body["batch"]["status"] == "staged"
        prev = body["batch"]["batch_hash"]
        h1, h2 = (h1, prev) if no == 2 else (prev, h2)

    # 封存
    r = client.post("/api/v1/seals", json={"source_id": sid})
    assert r.status_code == 200, r.text
    seal = r.json()
    assert seal["to_batch_no"] == 2
    assert seal["sealed_batch_nos"] == [1, 2]
    assert len(seal["seal_root"]) == 64

    # 封存状态查询
    r = client.get(f"/api/v1/sources/{sid}/seal")
    assert r.json()["sealed_to"] == 2
    assert r.json()["seal_root"] == seal["seal_root"]

    # 取事件证明并校验
    proof = client.get(
        f"/api/v1/batches/{sid}/1/proof?event_index=1"
    ).json()
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
    v = r.json()
    assert v["valid"] is True
    assert v["event_inclusion_valid"] is True
    assert v["batch_inclusion_valid"] is True
    assert v["seal_binding_valid"] is True
    assert v["anchor_matches_server"] is True


def test_out_of_order_staging_and_gap_report(client):
    sid = "src-gap"
    # 只到 #3，缺 #1 #2（#3 的 prev_hash 随便给一个合法 hex）
    fake = hashlib.sha256(b"x").hexdigest()
    _submit(client, sid, 3, fake, _events(1), 201)

    gaps = client.get(f"/api/v1/sources/{sid}/gaps").json()
    assert gaps["missing_batch_nos"] == [1, 2]
    assert gaps["next_expected_batch_no"] == 1
    assert gaps["staged_batch_nos"] == [3]
    assert gaps["chain_complete"] is False

    # 链未闭合不能封存
    r = client.post("/api/v1/seals", json={"source_id": sid})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "chain_not_complete"
    assert r.json()["error"]["details"]["missing_batch_nos"] == [1, 2]

    # 补 #1
    r = _submit(client, sid, 1, GENESIS_HASH, _events(2), 201)
    h1 = r.json()["batch"]["batch_hash"]
    gaps = client.get(f"/api/v1/sources/{sid}/gaps").json()
    assert gaps["missing_batch_nos"] == [2]

    # 补 #2（prev 正确），但 #3 早先声明的 prev_hash 是 fake，
    # 服务端通过后继批次的声明立即检出冲突并明确报错
    r = _submit(client, sid, 2, h1, _events(2))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "chain_conflict"
    assert fake in r.json()["error"]["message"]

    # #2 未入库，链仍不完整，封存报缺口
    gaps = client.get(f"/api/v1/sources/{sid}/gaps").json()
    assert gaps["missing_batch_nos"] == [2]
    r = client.post("/api/v1/seals", json={"source_id": sid})
    assert r.status_code == 409
    assert r.json()["error"]["details"]["missing_batch_nos"] == [2]


def test_idempotent_retry_returns_200(client):
    sid = "src-idem"
    payload = {
        "source_id": sid,
        "batch_no": 1,
        "prev_hash": GENESIS_HASH,
        "events": _events(2),
    }
    r1 = client.post("/api/v1/batches", json=payload)
    assert r1.status_code == 201
    r2 = client.post("/api/v1/batches", json=payload)
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r2.json()["batch"]["batch_hash"] == r1.json()["batch"]["batch_hash"]


def test_conflicting_duplicate_rejected(client):
    sid = "src-dup-conflict"
    _submit(client, sid, 1, GENESIS_HASH, _events(1), 201)
    r = _submit(client, sid, 1, GENESIS_HASH, _events(2))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_conflict"


def test_prev_hash_mismatch_against_known_predecessor(client):
    sid = "src-prev"
    h1 = _submit(client, sid, 1, GENESIS_HASH, _events(1), 201)
    h1 = h1.json()["batch"]["batch_hash"]
    bad = hashlib.sha256(b"bad").hexdigest()
    r = _submit(client, sid, 2, bad, _events(1))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "prev_hash_mismatch"
    assert h1 in r.json()["error"]["message"]


def test_successor_conflict_when_filling_gap(client):
    sid = "src-fill"
    fake = hashlib.sha256(b"fake-h2").hexdigest()
    # #3 先到，声明其前驱（#2）的哈希为 fake
    _submit(client, sid, 3, fake, _events(1), 201)
    # #1 到达（此时其后继 #2 尚不存在）
    r1 = _submit(client, sid, 1, GENESIS_HASH, _events(1), 201)
    h1 = r1.json()["batch"]["batch_hash"]
    # #2 到达：prev 指向 #1，但自身哈希几乎不可能等于 fake -> 冲突
    r = _submit(client, sid, 2, h1, _events(1, "distinct"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "chain_conflict"
    assert fake in r.json()["error"]["message"]


def test_bad_genesis_anchor_rejected(client):
    r = _submit(
        client, "src-gen", 1, hashlib.sha256(b"x").hexdigest(), _events(1)
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "bad_genesis_anchor"


def test_partial_seal_to_explicit_no(client):
    sid = "src-partial"
    prev = GENESIS_HASH
    for no in (1, 2, 3):
        r = _submit(client, sid, no, prev, _events(1, f"b{no}"), 201)
        prev = r.json()["batch"]["batch_hash"]

    seal = client.post(
        "/api/v1/seals", json={"source_id": sid, "to_batch_no": 2}
    ).json()
    assert seal["to_batch_no"] == 2
    assert seal["sealed_batch_nos"] == [1, 2]

    # #3 仍为暂存；继续封存到 3
    r = client.post("/api/v1/seals", json={"source_id": sid})
    assert r.json()["to_batch_no"] == 3

    # 幂等：to_batch_no=2 回显现有封存
    r = client.post("/api/v1/seals", json={"source_id": sid, "to_batch_no": 2})
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["to_batch_no"] == 3


def test_incremental_seal_tree_covers_full_history(client):
    """封到 #2 后再封 #3：#1 必须仍在新封存批次树中可证明。"""
    sid = "src-incr"
    prev = GENESIS_HASH
    for no in (1, 2, 3):
        r = _submit(client, sid, no, prev, _events(1, f"b{no}"), 201)
        prev = r.json()["batch"]["batch_hash"]

    client.post("/api/v1/seals", json={"source_id": sid, "to_batch_no": 2})
    proof1_old = client.get(
        f"/api/v1/batches/{sid}/1/proof?event_index=0"
    ).json()

    client.post("/api/v1/seals", json={"source_id": sid, "to_batch_no": 3})
    proof1_new = client.get(
        f"/api/v1/batches/{sid}/1/proof?event_index=0"
    ).json()

    # 新封存区间扩展为 1..3，#1 的批次证明依然有效
    assert proof1_new["seal"]["to_batch_no"] == 3
    r = client.post("/api/v1/verify", json={
        "source_id": sid,
        "batch_no": 1,
        "prev_hash": proof1_new["batch"]["prev_hash"],
        "event": proof1_new["event"],
        "event_index": 0,
        "event_proof": proof1_new["event_proof"],
        "events_root": proof1_new["events_root"],
        "batch_proof": proof1_new["batch"]["batch_proof"],
        "seal_from": 1,
        "seal_to": 3,
        "batch_tree_root": proof1_new["seal"]["batch_tree_root"],
        "seal_root": proof1_new["seal"]["seal_root"],
    })
    assert r.json()["valid"] is True

    # 旧证明的锚点（to=2）已不是服务端当前锚点
    r = client.post("/api/v1/verify", json={
        "source_id": sid,
        "batch_no": 1,
        "prev_hash": proof1_old["batch"]["prev_hash"],
        "event": proof1_old["event"],
        "event_index": 0,
        "event_proof": proof1_old["event_proof"],
        "events_root": proof1_old["events_root"],
        "batch_proof": proof1_old["batch"]["batch_proof"],
        "seal_from": 1,
        "seal_to": 2,
        "batch_tree_root": proof1_old["seal"]["batch_tree_root"],
        "seal_root": proof1_old["seal"]["seal_root"],
    })
    v = r.json()
    assert v["valid"] is True  # 旧证明在密码学上仍自洽
    assert v["anchor_matches_server"] is False  # 但已不是最新锚点


def test_seal_root_is_deterministic_and_isolated_per_source(client):
    def build(sid, tag):
        prev = GENESIS_HASH
        for no in (1, 2):
            r = _submit(client, sid, no, prev, _events(2, tag), 201)
            prev = r.json()["batch"]["batch_hash"]
        return client.post("/api/v1/seals", json={"source_id": sid}).json()

    s1 = build("det-1", "x")
    # 重复封存放回同一封存记录（幂等），根不变
    s2 = client.post("/api/v1/seals", json={"source_id": "det-1"}).json()
    assert s2["idempotent"] is True
    assert s2["seal_root"] == s1["seal_root"]

    # 同结构内容、不同 source_id -> 根必须不同
    other = build("det-2", "x")
    assert other["seal_root"] != s1["seal_root"]

    # 同 source_id 同内容重建于另一个库（此处仅校验绑定语义常量）：
    # seal_root 是 (source_id, 区间, batch_tree_root) 的确定性函数
    seal_state = client.get("/api/v1/sources/det-1/seal").json()
    assert seal_state["seal_root"] == s1["seal_root"]


def test_proof_tamper_detected(client):
    sid = "src-tamper"
    prev = GENESIS_HASH
    r = _submit(client, sid, 1, prev, _events(2), 201)

    client.post("/api/v1/seals", json={"source_id": sid})
    proof = client.get(f"/api/v1/batches/{sid}/1/proof?event_index=0").json()

    tampered = dict(proof)
    tampered_event = dict(proof["event"])
    tampered_event["payload"] = "forged"
    r = client.post("/api/v1/verify", json={
        "source_id": sid,
        "batch_no": 1,
        "prev_hash": proof["batch"]["prev_hash"],
        "event": tampered_event,
        "event_index": 0,
        "event_proof": proof["event_proof"],
        "events_root": proof["events_root"],
        "batch_proof": proof["batch"]["batch_proof"],
        "seal_from": 1,
        "seal_to": 1,
        "batch_tree_root": proof["seal"]["batch_tree_root"],
        "seal_root": proof["seal"]["seal_root"],
    })
    assert r.json()["valid"] is False
    assert r.json()["event_inclusion_valid"] is False


def test_proof_unavailable_before_seal(client):
    sid = "src-unsealed"
    _submit(client, sid, 1, GENESIS_HASH, _events(1), 201)
    r = client.get(f"/api/v1/batches/{sid}/1/proof?event_index=0")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_sealed"


def test_validation_errors(client):
    r = client.post("/api/v1/batches", json={
        "source_id": "s", "batch_no": 0,
        "prev_hash": GENESIS_HASH, "events": [{"a": 1}],
    })
    assert r.status_code == 422

    r = client.post("/api/v1/batches", json={
        "source_id": "s", "batch_no": 1,
        "prev_hash": GENESIS_HASH, "events": [],
    })
    assert r.status_code == 422

    r = client.post("/api/v1/batches", json={
        "source_id": "s", "batch_no": 1,
        "prev_hash": "xyz", "events": [1],
    })
    assert r.status_code == 422
