"""业务服务层：提交暂存、缺口查询、链闭合封存、包含证明与校验。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from . import db
from .errors import ConflictError, NotFoundError, ValidationError
from .hashing import (
    GENESIS_HASH,
    batch_hash as compute_batch_hash,
    bind_seal_root,
    canonical_json,
    event_leaf,
    merkle_proof,
    merkle_root,
    seal_roots,
    sha256_hex,
    verify_merkle_proof,
)


# ---------------------------------------------------------------- 内部工具

def _row_to_batch_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source_id": row["source_id"],
        "batch_no": row["batch_no"],
        "prev_hash": row["prev_hash"],
        "batch_hash": row["batch_hash"],
        "events_root": row["events_root"],
        "event_count": row["event_count"],
        "status": row["status"],
        "created_at": row["created_at"],
        "sealed_at": row["sealed_at"],
    }


def _sealed_to(conn: sqlite3.Connection, source_id: str) -> int:
    seal = db.get_seal(conn, source_id)
    return seal["sealed_to"] if seal else 0


def _next_expected(conn: sqlite3.Connection, source_id: str) -> int:
    """从当前封存水位出发，依据已暂存批次找到最长连续前缀的下一个缺口序号。"""
    expected = _sealed_to(conn, source_id) + 1
    while True:
        row = db.get_batch(conn, source_id, expected)
        if row is None:
            return expected
        expected += 1


def _missing_after(conn: sqlite3.Connection, source_id: str, start: int) -> list[int]:
    """从 start（含）起，依据已有批次找出全部缺口号（遇到缺口后继续扫描空洞）。"""
    rows = db.list_batches(conn, source_id)
    present = {r["batch_no"] for r in rows if r["batch_no"] >= start}
    if not present:
        return []
    return [n for n in range(start, max(present) + 1) if n not in present]


def _row_events(row: sqlite3.Row) -> list[Any]:
    return json.loads(row["events_json"])


# ---------------------------------------------------------------- 提交

def submit_batch(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    batch_no: int,
    prev_hash: str,
    events: list[Any],
) -> dict[str, Any]:
    """提交一个批次（允许乱序、允许重试）。

    返回 {"duplicate": bool, "batch": {...}}：
    * 完全相同的重复提交（幂等重试） -> duplicate=True，不报错
    * 同序号但内容/prev_hash 不同    -> 409 Conflict
    * 与已存在前驱的 prev_hash 不匹配 -> 409 Conflict
    """
    if batch_no < 1:
        raise ValidationError("batch_no 必须为 >= 1 的整数")
    if not isinstance(prev_hash, str) or len(prev_hash) != 64:
        raise ValidationError("prev_hash 必须为 64 位十六进制 SHA-256 字符串")
    try:
        int(prev_hash, 16)
    except ValueError:
        raise ValidationError("prev_hash 必须为十六进制字符串")
    if not isinstance(events, list) or not events:
        raise ValidationError("events 必须为非空数组")

    expected_prev = GENESIS_HASH if batch_no == 1 else None
    leaf_hashes = [event_leaf(e) for e in events]
    root = merkle_root(leaf_hashes)
    new_batch_hash = compute_batch_hash(source_id, batch_no, prev_hash, events)

    with db.lock():
        existing = db.get_batch(conn, source_id, batch_no)
        if existing is not None:
            return _handle_duplicate(conn, existing, prev_hash, events, new_batch_hash)

        # 创世批次必须锚定 sha256("")
        if batch_no == 1 and prev_hash != GENESIS_HASH:
            raise ConflictError(
                f"创世批次(batch_no=1)的 prev_hash 必须为 {GENESIS_HASH}",
                code="bad_genesis_anchor",
            )

        # 前驱已存在：立即校验链指针，尽早暴露冲突
        if batch_no > 1:
            pred = db.get_batch(conn, source_id, batch_no - 1)
            if pred is not None and prev_hash != pred["batch_hash"]:
                raise ConflictError(
                    f"prev_hash 与已接收的批次 {batch_no - 1} 不匹配："
                    f"期望 {pred['batch_hash']}，实际 {prev_hash}",
                    code="prev_hash_mismatch",
                )

            # 后继已存在（乱序补洞）：该批次必须正好是后继声明的前驱
            succ = db.get_batch(conn, source_id, batch_no + 1)
            if succ is not None and succ["prev_hash"] != new_batch_hash:
                raise ConflictError(
                    f"批次冲突：已接收的后继批次 {batch_no + 1} 声明的前驱哈希为 "
                    f"{succ['prev_hash']}，当前批次哈希为 {new_batch_hash}",
                    code="chain_conflict",
                )

        # 已封存水位之前不允许插入
        sealed_to = _sealed_to(conn, source_id)
        if batch_no <= sealed_to:
            # 理论上与 existing 分支重叠，防御性处理
            raise ConflictError(
                f"批次 {batch_no} 已封存，不可修改", code="batch_sealed"
            )

        db.insert_batch(
            conn,
            source_id=source_id,
            batch_no=batch_no,
            prev_hash=prev_hash,
            batch_hash=new_batch_hash,
            events_root=root,
            events=events,
        )
        conn.commit()

        next_expected = _next_expected(conn, source_id)
        result = {
            "duplicate": False,
            "batch": {
                "source_id": source_id,
                "batch_no": batch_no,
                "prev_hash": prev_hash,
                "batch_hash": new_batch_hash,
                "events_root": root,
                "event_count": len(events),
                "status": "staged",
            },
            "next_expected_batch_no": next_expected,
        }
        return result


def _handle_duplicate(
    conn: sqlite3.Connection,
    existing: sqlite3.Row,
    prev_hash: str,
    events: list[Any],
    new_batch_hash: str,
) -> dict[str, Any]:
    same = (
        existing["prev_hash"] == prev_hash
        and existing["batch_hash"] == new_batch_hash
    )
    if not same:
        raise ConflictError(
            f"批次 {existing['batch_no']} 已存在但内容/prev_hash 不一致："
            f"已存 batch_hash={existing['batch_hash']}，"
            f"提交 batch_hash={new_batch_hash}",
            code="duplicate_conflict",
        )
    # 幂等重试：原样回显
    return {
        "duplicate": True,
        "batch": _row_to_batch_dict(existing),
        "next_expected_batch_no": _next_expected(conn, existing["source_id"]),
    }


# ---------------------------------------------------------------- 缺口 / 状态

def get_gaps(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    with db.lock():
        sealed_to = _sealed_to(conn, source_id)
        rows = db.list_batches(conn, source_id)
        present = {r["batch_no"] for r in rows}
        start = sealed_to + 1
        missing = _missing_after(conn, source_id, start)
        next_expected = _next_expected(conn, source_id)
        staged = sorted(n for n in present if n > sealed_to)
        return {
            "source_id": source_id,
            "sealed_to": sealed_to,
            "next_expected_batch_no": next_expected,
            "staged_batch_nos": staged,
            "missing_batch_nos": missing,
            "chain_complete": next_expected > max(present) if present else False,
        }


def list_statuses(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for source_id in db.list_sources(conn):
        gaps = get_gaps(conn, source_id)
        seal = db.get_seal(conn, source_id)
        out.append(
            {
                **gaps,
                "seal_root": seal["seal_root"] if seal else None,
                "sealed_at": seal["sealed_at"] if seal else None,
            }
        )
    return out


def get_batch_detail(
    conn: sqlite3.Connection, source_id: str, batch_no: int
) -> dict[str, Any]:
    with db.lock():
        row = db.get_batch(conn, source_id, batch_no)
        if row is None:
            raise NotFoundError(f"批次 {source_id}#{batch_no} 不存在")
        detail = _row_to_batch_dict(row)
        detail["events"] = _row_events(row)
        return detail


# ---------------------------------------------------------------- 封存

def seal(
    conn: sqlite3.Connection, source_id: str, to_batch_no: Optional[int] = None
) -> dict[str, Any]:
    """封存从 sealed_to+1 开始的最长连续闭合链（或封到 to_batch_no）。

    封存时逐批复算链指针与批次哈希；任一缺口/断链/篡改 -> 409。
    to_batch_no 落在已封存区间 -> 幂等返回现有封存记录。
    """
    with db.lock():
        sealed_to = _sealed_to(conn, source_id)

        if to_batch_no is not None and to_batch_no <= sealed_to:
            if to_batch_no < 1:
                raise ValidationError("to_batch_no 必须 >= 1")
            existing_seal = db.get_seal(conn, source_id)
            return {
                "idempotent": True,
                "source_id": source_id,
                "from_batch_no": 1,
                "to_batch_no": existing_seal["sealed_to"],
                "batch_tree_root": existing_seal["batch_tree_root"],
                "seal_root": existing_seal["seal_root"],
                "sealed_at": existing_seal["sealed_at"],
            }

        # 找出可封存的连续前缀
        target = sealed_to
        rows_by_no: dict[int, sqlite3.Row] = {}
        cursor_no = sealed_to + 1
        while True:
            row = db.get_batch(conn, source_id, cursor_no)
            if row is None:
                break
            target = cursor_no
            rows_by_no[cursor_no] = row
            cursor_no += 1

        if target == sealed_to:
            # 已有封存但没有新的连续批次：幂等回显
            if sealed_to > 0:
                existing_seal = db.get_seal(conn, source_id)
                return {
                    "idempotent": True,
                    "source_id": source_id,
                    "from_batch_no": 1,
                    "to_batch_no": existing_seal["sealed_to"],
                    "batch_tree_root": existing_seal["batch_tree_root"],
                    "seal_root": existing_seal["seal_root"],
                    "sealed_at": existing_seal["sealed_at"],
                }
            missing = _missing_after(conn, source_id, sealed_to + 1)
            raise ConflictError(
                "链未闭合，无法封存："
                + (
                    f"从批次 {sealed_to + 1} 起尚无连续批次，缺口 {missing}"
                    if missing
                    else f"暂无可封存批次（下一个期望批次 {sealed_to + 1}）"
                ),
                code="chain_not_complete",
                details={"missing_batch_nos": missing},
            )

        all_rows = db.list_batches(conn, source_id)
        max_present = max(r["batch_no"] for r in all_rows)

        # 未显式指定 to_batch_no 时，要求“整条暂存链”闭合：
        # 即最高批次号之前不得存在空洞；
        # 显式 to_batch_no 则允许只封存到连续前缀（但不得越过缺口）。
        if to_batch_no is None:
            if target < max_present:
                missing = _missing_after(conn, source_id, target + 1)
                raise ConflictError(
                    f"链未闭合，无法封存：批次链在 {target + 1} 处存在缺口，"
                    f"已暂存的更高批次 {sorted(n for n in (r['batch_no'] for r in all_rows) if n > target)} 将悬空",
                    code="chain_not_complete",
                    details={"missing_batch_nos": missing},
                )
        elif to_batch_no > target:
            missing = _missing_after(conn, source_id, target + 1)
            raise ConflictError(
                f"无法封存至 {to_batch_no}：链仅连续到 {target}",
                code="chain_not_complete",
                details={"missing_batch_nos": missing},
            )
        else:
            target = to_batch_no

        # 逐批复算链与批次哈希（防止库内数据被篡改）。
        # 必须覆盖 1..target 的全部批次（含此前已封存部分），
        # 因为封存批次树的叶子集合就是该区间内所有批次。
        prev_h = GENESIS_HASH
        batch_hashes: list[str] = []
        for no in range(1, target + 1):
            row = rows_by_no.get(no) or db.get_batch(conn, source_id, no)
            events = _row_events(row)
            if row["prev_hash"] != prev_h:
                raise ConflictError(
                    f"批次 {no} 断链：prev_hash 应为 {prev_h}，"
                    f"实际 {row['prev_hash']}",
                    code="broken_chain",
                )
            recomputed = compute_batch_hash(
                source_id, no, row["prev_hash"], events
            )
            if recomputed != row["batch_hash"]:
                raise ConflictError(
                    f"批次 {no} 内容与存储哈希不一致（疑似篡改）",
                    code="tampered_batch",
                )
            prev_h = row["batch_hash"]
            batch_hashes.append(row["batch_hash"])

        tree_root, root = seal_roots(source_id, 1, target, batch_hashes)
        when = db.utcnow()
        for no in range(sealed_to + 1, target + 1):
            db.mark_sealed(conn, source_id, no, when)
        db.upsert_seal(
            conn,
            source_id=source_id,
            sealed_to=target,
            batch_tree_root=tree_root,
            root=root,
            when=when,
        )
        conn.commit()

        return {
            "idempotent": False,
            "source_id": source_id,
            "from_batch_no": 1,
            "to_batch_no": target,
            "batch_tree_root": tree_root,
            "seal_root": root,
            "sealed_batch_nos": list(range(sealed_to + 1, target + 1)),
            "sealed_at": when,
        }


def get_seal_status(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    with db.lock():
        seal = db.get_seal(conn, source_id)
        gaps = get_gaps(conn, source_id)
        if seal is None:
            return {
                "source_id": source_id,
                "sealed_to": 0,
                "seal_root": None,
                "sealed_at": None,
                **gaps,
            }
        return {
            "source_id": source_id,
            "sealed_to": seal["sealed_to"],
            "seal_root": seal["seal_root"],
            "sealed_at": seal["sealed_at"],
            **{k: v for k, v in gaps.items() if k != "source_id"},
        }


# ---------------------------------------------------------------- 证明 / 校验

def get_proof(
    conn: sqlite3.Connection, source_id: str, batch_no: int, event_index: int
) -> dict[str, Any]:
    with db.lock():
        row = db.get_batch(conn, source_id, batch_no)
        if row is None:
            raise NotFoundError(f"批次 {source_id}#{batch_no} 不存在")
        if row["status"] != "sealed":
            raise ConflictError(
                f"批次 {batch_no} 尚未封存，封存后才能出具校验证明",
                code="not_sealed",
            )
        events = _row_events(row)
        if event_index < 0 or event_index >= len(events):
            raise ValidationError(
                f"event_index 越界：批次 {batch_no} 共 {len(events)} 个事件"
            )

        leaves = [event_leaf(e) for e in events]
        proof = merkle_proof(leaves, event_index)
        seal = db.get_seal(conn, source_id)

        # 批次级证明：该批次哈希在封存批次树中的路径（对 batch_tree_root 验证）
        sealed_hashes: list[str] = []
        for no in range(1, seal["sealed_to"] + 1):
            r = db.get_batch(conn, source_id, no)
            sealed_hashes.append(r["batch_hash"])
        batch_level_proof = merkle_proof(sealed_hashes, batch_no - 1)

        return {
            "source_id": source_id,
            "batch_no": batch_no,
            "event_index": event_index,
            "event": events[event_index],
            "event_leaf": leaves[event_index],
            "events_root": row["events_root"],
            "event_proof": proof,
            "batch": {
                "prev_hash": row["prev_hash"],
                "batch_hash": row["batch_hash"],
                "batch_proof": batch_level_proof,
            },
            "seal": {
                "from_batch_no": 1,
                "to_batch_no": seal["sealed_to"],
                "batch_tree_root": seal["batch_tree_root"],
                "seal_root": seal["seal_root"],
            },
        }


def verify_proof(
    conn: Optional[sqlite3.Connection],
    *,
    source_id: str,
    batch_no: int,
    prev_hash: str,
    event: Any,
    event_index: int,
    event_proof: list[dict[str, str]],
    events_root: str,
    batch_proof: list[dict[str, str]],
    seal_from: int,
    seal_to: int,
    batch_tree_root: str,
    seal_root_value: str,
) -> dict[str, Any]:
    """纯密码学复核（不依赖服务端存储），并可选与库中封存锚点比对。

    校验三层：
    1. 事件包含于批次 events_root；
    2. 批次包含于封存批次树 batch_tree_root；
    3. seal_root == bind(source_id, 区间, batch_tree_root)，防跨来源/区间重放。
    """
    leaf = event_leaf(event)
    event_ok = verify_merkle_proof(leaf, event_proof, events_root)

    # 批次哈希 = H(BATCH || canonical(元数据 + 整批 events_root))。
    # events_root 由调用方随证明提供，并被 batch_tree_root（经封存）间接提交，
    # 因此伪造的 events_root 无法通过下一层批次包含证明。
    b_hash = sha256_hex(
        b"\x00BATCH"
        + canonical_json(
            {
                "source_id": source_id,
                "batch_no": batch_no,
                "prev_hash": prev_hash,
                "events_root": events_root,
            }
        )
    )
    batch_ok = verify_merkle_proof(b_hash, batch_proof, batch_tree_root)
    range_ok = seal_from <= batch_no <= seal_to and seal_from == 1
    rebind_root = bind_seal_root(source_id, seal_from, seal_to, batch_tree_root)
    seal_ok = rebind_root == seal_root_value

    anchor_ok: Optional[bool] = None
    if conn is not None:
        seal = db.get_seal(conn, source_id)
        anchor_ok = (
            seal is not None
            and seal["sealed_to"] == seal_to
            and seal["batch_tree_root"] == batch_tree_root
            and seal["seal_root"] == seal_root_value
        )

    return {
        "valid": bool(event_ok and batch_ok and range_ok and seal_ok),
        "event_inclusion_valid": event_ok,
        "batch_inclusion_valid": batch_ok,
        "batch_range_valid": range_ok,
        "seal_binding_valid": seal_ok,
        "anchor_matches_server": anchor_ok,
        "recomputed_event_leaf": leaf,
        "recomputed_batch_hash": b_hash,
        "recomputed_seal_root": rebind_root,
    }
