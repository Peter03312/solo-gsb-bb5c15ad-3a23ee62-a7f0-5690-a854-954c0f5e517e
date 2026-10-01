"""SQLite 存储层。

两张核心表：

* batches —— 每个 (source_id, batch_no) 一行，记录链式状态与暂存/封存状态
* seals   —— 每个 source_id 一行，记录当前封存水位与封存 Merkle 根
* contract_versions    —— 每个 (source_id, version) 一行，不可变的事件契约
* contract_activations —— 每个 source_id 一行，指向当前生效契约版本（原子切换）

写入均在进程级互斥锁 + 单写事务内完成，保证多采集端并发提交/封存时
链状态判断的一致性。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    batch_no        INTEGER NOT NULL,
    prev_hash       TEXT NOT NULL,
    batch_hash      TEXT NOT NULL,
    events_root     TEXT NOT NULL,
    events_json     TEXT NOT NULL,
    event_count     INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('staged', 'sealed')),
    created_at      TEXT NOT NULL,
    sealed_at       TEXT,
    UNIQUE (source_id, batch_no)
);

CREATE TABLE IF NOT EXISTS seals (
    source_id       TEXT PRIMARY KEY,
    sealed_to       INTEGER NOT NULL,
    batch_tree_root TEXT NOT NULL,
    seal_root       TEXT NOT NULL,
    sealed_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contract_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    version         TEXT NOT NULL,
    schema_json     TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (source_id, version)
);

CREATE TABLE IF NOT EXISTS contract_activations (
    source_id       TEXT PRIMARY KEY,
    version         TEXT NOT NULL,
    activated_at    TEXT NOT NULL,
    UNIQUE (source_id, version),
    FOREIGN KEY (source_id, version)
        REFERENCES contract_versions (source_id, version)
);
"""

_write_lock = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db_path() -> str:
    return os.environ.get("EVIDENCE_DB", "/data/evidence.db")


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _write_lock:
        conn.executescript(_SCHEMA)
        # 旧库迁移：批次记录提交时声明的契约版本（无契约为 NULL）
        existing_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(batches)").fetchall()
        }
        if "contract_version" not in existing_cols:
            conn.execute("ALTER TABLE batches ADD COLUMN contract_version TEXT")
        conn.commit()


def lock() -> threading.RLock:
    """供服务层串行化读-判-写流程。"""
    return _write_lock


def get_batch(conn: sqlite3.Connection, source_id: str, batch_no: int) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM batches WHERE source_id = ? AND batch_no = ?",
        (source_id, batch_no),
    )
    return cur.fetchone()


def list_batches(conn: sqlite3.Connection, source_id: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM batches WHERE source_id = ? ORDER BY batch_no",
        (source_id,),
    )
    return cur.fetchall()


def list_sources(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT DISTINCT source_id FROM batches ORDER BY source_id")
    return [r["source_id"] for r in cur.fetchall()]


def get_seal(conn: sqlite3.Connection, source_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM seals WHERE source_id = ?", (source_id,))
    return cur.fetchone()


def insert_batch(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    batch_no: int,
    prev_hash: str,
    batch_hash: str,
    events_root: str,
    events: list[Any],
    contract_version: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO batches
            (source_id, batch_no, prev_hash, batch_hash, events_root,
             events_json, event_count, status, created_at, contract_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?)
        """,
        (
            source_id,
            batch_no,
            prev_hash,
            batch_hash,
            events_root,
            json.dumps(events, ensure_ascii=False, separators=(",", ":")),
            len(events),
            utcnow(),
            contract_version,
        ),
    )


def mark_sealed(conn: sqlite3.Connection, source_id: str, batch_no: int, when: str) -> None:
    conn.execute(
        "UPDATE batches SET status = 'sealed', sealed_at = ? "
        "WHERE source_id = ? AND batch_no = ?",
        (when, source_id, batch_no),
    )


def upsert_seal(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    sealed_to: int,
    batch_tree_root: str,
    root: str,
    when: str,
) -> None:
    conn.execute(
        """
        INSERT INTO seals (source_id, sealed_to, batch_tree_root, seal_root, sealed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            sealed_to = excluded.sealed_to,
            batch_tree_root = excluded.batch_tree_root,
            seal_root = excluded.seal_root,
            sealed_at = excluded.sealed_at
        """,
        (source_id, sealed_to, batch_tree_root, root, when),
    )


# ---------------------------------------------------------------- 契约

def get_contract(
    conn: sqlite3.Connection, source_id: str, version: str
) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM contract_versions WHERE source_id = ? AND version = ?",
        (source_id, version),
    )
    return cur.fetchone()


def list_contracts(conn: sqlite3.Connection, source_id: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM contract_versions WHERE source_id = ? ORDER BY id",
        (source_id,),
    )
    return cur.fetchall()


def list_contract_sources(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute(
        "SELECT DISTINCT source_id FROM contract_versions ORDER BY source_id"
    )
    return [r["source_id"] for r in cur.fetchall()]


def insert_contract(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    version: str,
    schema: dict[str, Any],
    content_hash: str,
) -> None:
    conn.execute(
        """
        INSERT INTO contract_versions (source_id, version, schema_json,
                                       content_hash, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            source_id,
            version,
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            content_hash,
            utcnow(),
        ),
    )


def get_active_contract(
    conn: sqlite3.Connection, source_id: str
) -> Optional[sqlite3.Row]:
    """返回当前生效契约（activations JOIN versions）；未激活/未知来源均为 None。"""
    cur = conn.execute(
        """
        SELECT v.*, a.activated_at
        FROM contract_activations AS a
        JOIN contract_versions AS v
          ON v.source_id = a.source_id AND v.version = a.version
        WHERE a.source_id = ?
        """,
        (source_id,),
    )
    return cur.fetchone()


def activate_contract(
    conn: sqlite3.Connection, *, source_id: str, version: str, when: str
) -> None:
    """单行 upsert：同一写事务内原子切换生效版本。"""
    conn.execute(
        """
        INSERT INTO contract_activations (source_id, version, activated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            version = excluded.version,
            activated_at = excluded.activated_at
        """,
        (source_id, version, when),
    )
