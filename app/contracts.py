"""来源事件契约：JSON Schema 子集注册/激活，与按契约逐事件校验。

支持的 Schema 关键字（子集）：``type``、``required``、``properties``、``items``、
``enum``。其余关键字一律拒绝，避免“注册成功但校验语义被静默忽略”。

校验错误以 **JSONPath（点号路径）** 定位事件内字段；根路径用 ``$``，
数组下标用 ``[i]``，例如 ``$.events[2].payload.value`` 表示第 3 个事件的
``payload.value`` 字段。
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Optional

from . import db
from .errors import ConflictError, NotFoundError, ValidationError
from .hashing import contract_content_hash

# type 取值 -> Python 判定
_ALLOWED_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_SUPPORTED_KEYWORDS = {"type", "required", "properties", "items", "enum"}


# ---------------------------------------------------------------- 路径工具

def _prop_path(base: str, key: str) -> str:
    """对象属性的 JSONPath；简单标识符用点号，否则用括号引号转义。"""
    if key and all(c.isalnum() or c == "_" for c in key):
        return f"{base}.{key}"
    escaped = key.replace("\\", "\\\\").replace("'", "\\'")
    return f"{base}['{escaped}']"


def _index_path(base: str, idx: int) -> str:
    return f"{base}[{idx}]"


# ---------------------------------------------------------------- Schema 自检

class _SchemaProblem(ValueError):
    """注册的 Schema 自身不合法（区别于事件校验失败）。"""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def _validate_meta(schema: Any, path: str = "$") -> None:
    """对注册的 JSON Schema 子集做结构自检；任何不支持/畸形之处均报错。"""
    if not isinstance(schema, dict):
        raise _SchemaProblem(path, "schema 必须是对象")

    unknown = set(schema) - _SUPPORTED_KEYWORDS
    if unknown:
        raise _SchemaProblem(
            path,
            "仅支持关键字 type/required/properties/items/enum，"
            f"发现不支持的关键字：{sorted(unknown)}",
        )

    t = schema.get("type")
    if t is not None:
        if not isinstance(t, str):
            raise _SchemaProblem(_prop_path(path, "type"), "必须是字符串")
        if t not in _ALLOWED_TYPES:
            raise _SchemaProblem(
                _prop_path(path, "type"), f"取值必须是 {sorted(_ALLOWED_TYPES)} 之一"
            )

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not required:
            raise _SchemaProblem(
                _prop_path(path, "required"), "必须是非空字符串数组"
            )
        if not all(isinstance(r, str) and r for r in required):
            raise _SchemaProblem(
                _prop_path(path, "required"), "每一项必须是非空字符串"
            )
        if len(set(required)) != len(required):
            raise _SchemaProblem(
                _prop_path(path, "required"), "字段名不得重复"
            )

    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict) or not props:
            raise _SchemaProblem(
                _prop_path(path, "properties"), "必须是非空对象（字段名 -> 子 schema）"
            )
        for key, sub in props.items():
            _validate_meta(sub, _prop_path(path, key))

    if "items" in schema:
        _validate_meta(schema["items"], _prop_path(path, "items"))

    if "enum" in schema:
        allowed = schema["enum"]
        if not isinstance(allowed, list) or not allowed:
            raise _SchemaProblem(
                _prop_path(path, "enum"), "必须是非空数组，列出允许的取值"
            )


def assert_valid_schema(schema: Any) -> None:
    """对外入口：把 Schema 自检异常转换为 422 API 错误。"""
    try:
        _validate_meta(schema)
    except _SchemaProblem as exc:
        raise ValidationError(
            f"契约 Schema 不合法（{exc.path}：{exc.message}）",
            code="invalid_contract_schema",
            details={"path": exc.path},
        )


# ---------------------------------------------------------------- 事件校验

def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "unknown"


def _type_matches(expected: str, value: Any) -> bool:
    actual = _json_type(value)
    if expected == "number":
        # JSON Schema: number 同时接受整数与浮点数；boolean 不属于 number
        return actual in ("number", "integer") and not isinstance(value, bool)
    if expected == "integer":
        # 拒绝 1.0 这类浮点：JSON 语义下它不是 integer
        return isinstance(value, int) and not isinstance(value, bool)
    return actual == expected


def _enum_equal(a: Any, b: Any) -> bool:
    """JSON 值相等：bool 不与数字混同；数字按数值比较，其余严格类型+值比较。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, float) and math.isnan(a):
            return isinstance(b, float) and math.isnan(b)
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_enum_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_enum_equal(x, y) for x, y in zip(a, b))
    return a == b


def _check(schema: dict[str, Any], value: Any, path: str, out: list[dict]) -> None:
    """按 schema 校验单个位置，收集全部违规；type 不符即停止深入嵌套关键字。"""
    expected_type = schema.get("type")
    if expected_type is not None and not _type_matches(expected_type, value):
        out.append(
            {
                "path": path,
                "rule": "type",
                "expected": expected_type,
                "actual": _json_type(value),
                "message": f"类型应为 {expected_type}，实际为 {_json_type(value)}",
            }
        )
        return

    if "enum" in schema and not any(_enum_equal(value, v) for v in schema["enum"]):
        out.append(
            {
                "path": path,
                "rule": "enum",
                "message": "取值不在 enum 允许范围内",
            }
        )

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(
                    {
                        "path": _prop_path(path, key),
                        "rule": "required",
                        "message": f"缺少必填字段 {key!r}",
                    }
                )
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                _check(sub, value[key], _prop_path(path, key), out)

    if isinstance(value, list) and "items" in schema:
        sub = schema["items"]
        for idx, item in enumerate(value):
            _check(sub, item, _index_path(path, idx), out)


def validate_events(schema: dict[str, Any], events: list[Any]) -> list[dict[str, Any]]:
    """逐事件校验整批事件，返回违规列表（空列表表示通过）。

    每项形如 ``{"event_index": i, "path": "$.events[i]....", ...}``。
    """
    violations: list[dict[str, Any]] = []
    for idx, event in enumerate(events):
        event_violations: list[dict[str, Any]] = []
        _check(schema, event, f"$.events[{idx}]", event_violations)
        for v in event_violations:
            violations.append({"event_index": idx, **v})
    return violations


# ---------------------------------------------------------------- 服务层

def _row_to_contract_dict(row: sqlite3.Row, *, with_schema: bool) -> dict[str, Any]:
    data = {
        "source_id": row["source_id"],
        "version": row["version"],
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
    }
    if with_schema:
        data["schema"] = json.loads(row["schema_json"])
    if "activated_at" in row.keys():
        data["activated_at"] = row["activated_at"]
    return data


def register_contract(
    conn: sqlite3.Connection, *, source_id: str, version: str, schema: dict[str, Any]
) -> dict[str, Any]:
    """注册（或幂等重试）一个来源事件契约版本。

    * 同 (source_id, version) 且 schema 结构一致 -> 幂等回显；
    * 同版本但 schema 内容不同 -> 409 contract_conflict。
    """
    assert_valid_schema(schema)
    content_hash = contract_content_hash(source_id, version, schema)

    with db.lock():
        existing = db.get_contract(conn, source_id, version)
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise ConflictError(
                    f"来源 {source_id} 的契约版本 {version!r} 已存在但内容不一致："
                    f"已存 content_hash={existing['content_hash']}，"
                    f"提交 content_hash={content_hash}",
                    code="contract_conflict",
                )
            return {"idempotent": True, **_row_to_contract_dict(existing, with_schema=True)}

        db.insert_contract(
            conn,
            source_id=source_id,
            version=version,
            schema=schema,
            content_hash=content_hash,
        )
        conn.commit()
        row = db.get_contract(conn, source_id, version)
        return {"idempotent": False, **_row_to_contract_dict(row, with_schema=True)}


def activate_contract(
    conn: sqlite3.Connection, *, source_id: str, version: str
) -> dict[str, Any]:
    """原子切换某来源的生效契约版本；重复激活同一版本幂等。"""
    with db.lock():
        row = db.get_contract(conn, source_id, version)
        if row is None:
            raise NotFoundError(
                f"来源 {source_id} 不存在契约版本 {version!r}，请先注册",
            )

        current = db.get_active_contract(conn, source_id)
        already_active = current is not None and current["version"] == version
        when = current["activated_at"] if already_active else db.utcnow()
        # 单行 upsert：版本指针的读-改-写在同一锁/事务内，切换原子可见
        db.activate_contract(conn, source_id=source_id, version=version, when=when)
        conn.commit()

        active = db.get_active_contract(conn, source_id)
        return {
            "idempotent": already_active,
            **_row_to_contract_dict(active, with_schema=True),
        }


def enforce(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    contract_version: Optional[str],
    events: list[Any],
) -> None:
    """批次提交时按契约逐事件校验；任一事件失败整批拒绝（422）。

    * contract_version 未注册 -> 404 not_found
    * 已注册但不是生效版本   -> 409 contract_not_active
    * 事件不符合契约         -> 422 contract_violation（details 含 JSONPath）
    """
    row = db.get_contract(conn, source_id, contract_version)
    if row is None:
        raise NotFoundError(
            f"来源 {source_id} 不存在契约版本 {contract_version!r}，请先注册",
        )

    active = db.get_active_contract(conn, source_id)
    if active is None or active["version"] != contract_version:
        raise ConflictError(
            f"契约版本 {contract_version!r} 不是来源 {source_id} 的生效版本",
            code="contract_not_active",
            details={
                "requested_version": contract_version,
                "active_version": active["version"] if active else None,
            },
        )

    schema = json.loads(row["schema_json"])
    violations = validate_events(schema, events)
    if violations:
        raise ValidationError(
            f"{len(violations)} 处事件不符合契约版本 {contract_version!r}，整批拒绝",
            code="contract_violation",
            details={
                "source_id": source_id,
                "contract_version": contract_version,
                "violations": violations,
            },
        )


# ---------------------------------------------------------------- 查询

def list_contracts_for_source(
    conn: sqlite3.Connection, source_id: str
) -> dict[str, Any]:
    with db.lock():
        rows = db.list_contracts(conn, source_id)
        if not rows:
            raise NotFoundError(f"来源 {source_id} 未注册任何契约版本")
        active = db.get_active_contract(conn, source_id)
        active_version = active["version"] if active else None
        return {
            "source_id": source_id,
            "active_version": active_version,
            "versions": [
                {
                    **_row_to_contract_dict(r, with_schema=False),
                    "active": r["version"] == active_version,
                }
                for r in rows
            ],
        }


def list_all_contracts(conn: sqlite3.Connection) -> dict[str, Any]:
    with db.lock():
        sources = []
        for source_id in db.list_contract_sources(conn):
            sources.append(list_contracts_for_source(conn, source_id))
        return {"sources": sources}


def get_contract_detail(
    conn: sqlite3.Connection, source_id: str, version: str
) -> dict[str, Any]:
    with db.lock():
        row = db.get_contract(conn, source_id, version)
        if row is None:
            raise NotFoundError(
                f"来源 {source_id} 不存在契约版本 {version!r}",
            )
        active = db.get_active_contract(conn, source_id)
        return {
            **_row_to_contract_dict(row, with_schema=True),
            "active": active is not None and active["version"] == version,
        }


def get_active_contract_detail(
    conn: sqlite3.Connection, source_id: str
) -> dict[str, Any]:
    with db.lock():
        active = db.get_active_contract(conn, source_id)
        if active is None:
            # 区分未知来源与“已注册但尚未激活”，错误码更明确
            any_versions = db.list_contracts(conn, source_id)
            if not any_versions:
                raise NotFoundError(f"来源 {source_id} 未注册任何契约版本")
            raise NotFoundError(
                f"来源 {source_id} 已注册契约但尚无生效版本，请先激活",
            )
        return {**_row_to_contract_dict(active, with_schema=True), "active": True}


def active_version(conn: sqlite3.Connection, source_id: str) -> Optional[str]:
    """供来源状态查询附带当前生效契约版本。"""
    with db.lock():
        active = db.get_active_contract(conn, source_id)
        return active["version"] if active else None
