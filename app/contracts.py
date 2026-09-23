"""来源事件契约：受限 JSON Schema 子集的结构校验与逐事件实例校验。

支持的子集关键字（其余关键字一律拒绝，避免“静默忽略”造成的认知偏差）：

* ``type``       —— 单一类型字符串：object / array / string / number /
                    integer / boolean / null
* ``required``   —— object 必填键名数组（仅校验键存在）
* ``properties`` —— object 各属性的子模式（递归支持全部子集关键字）
* ``items``      —— array 每个元素的子模式（单一模式，不支持 tuple 形式）
* ``enum``       —— 任意类型值的非空枚举数组（按 JSON 值相等比较）

实例校验收集**全部**违规并返回 JSONPath（相对单个事件根，``$`` 为根），
供上层在批次被整批拒绝时一次性反馈。
"""
from __future__ import annotations

from typing import Any

ALLOWED_KEYS = {"type", "required", "properties", "items", "enum"}
SUPPORTED_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}

_TYPE_NAMES_PY: dict[str, str] = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    type(None): "null",
}


class SchemaSyntaxError(Exception):
    """契约模式本身不合法（注册期 422）。"""

    def __init__(self, violations: list[dict[str, str]]):
        super().__init__("contract schema 不是受支持的 JSON Schema 子集")
        self.violations = violations


# ---------------------------------------------------------------- 模式结构校验

def validate_schema(schema: Any) -> None:
    """注册前校验模式结构；不合法抛 :class:`SchemaSyntaxError`。"""
    violations: list[dict[str, str]] = []
    _validate_schema_node(schema, "", violations)
    if violations:
        raise SchemaSyntaxError(violations)


def _err(violations: list[dict[str, str]], path: str, message: str) -> None:
    violations.append({"schema_path": path or "/", "message": message})


def _validate_schema_node(
    node: Any, path: str, violations: list[dict[str, str]]
) -> None:
    if not isinstance(node, dict):
        _err(violations, path, "模式节点必须是 JSON 对象")
        return

    unknown = set(node) - ALLOWED_KEYS
    if unknown:
        _err(
            violations,
            path,
            f"不支持的 JSON Schema 关键字：{sorted(unknown)}；"
            f"仅允许 {sorted(ALLOWED_KEYS)}",
        )

    t = node.get("type")
    if "type" in node:
        if not isinstance(t, str):
            _err(violations, f"{path}/type", "type 必须是单一类型字符串")
        elif t not in SUPPORTED_TYPES:
            _err(
                violations,
                f"{path}/type",
                f"未知类型 {t!r}；支持 {sorted(SUPPORTED_TYPES)}",
            )

    if "required" in node:
        required = node["required"]
        if not isinstance(required, list) or not required:
            _err(violations, f"{path}/required", "required 必须是非空字符串数组")
        else:
            bad = [i for i, name in enumerate(required) if not isinstance(name, str)]
            dup = len(required) != len(set(required))
            if bad:
                _err(
                    violations,
                    f"{path}/required",
                    f"required 数组第 {bad[0]} 项不是字符串",
                )
            if dup:
                _err(violations, f"{path}/required", "required 中存在重复键名")

    if "properties" in node:
        props = node["properties"]
        if not isinstance(props, dict) or not props:
            _err(violations, f"{path}/properties", "properties 必须是非空对象")
        else:
            for name, sub in props.items():
                if not isinstance(name, str):
                    _err(violations, f"{path}/properties", "属性名必须是字符串")
                _validate_schema_node(sub, f"{path}/properties/{name}", violations)

    if "items" in node:
        _validate_schema_node(node["items"], f"{path}/items", violations)

    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum:
            _err(violations, f"{path}/enum", "enum 必须是非空数组")


# ---------------------------------------------------------------- 实例校验

def _json_type_name(value: Any) -> str:
    # bool 是 int 的子类，必须先判别
    if isinstance(value, bool):
        return "boolean"
    for py_type, name in _TYPE_NAMES_PY.items():
        if isinstance(value, py_type):
            return name
    return "unknown"


def _type_matches(expected: str, value: Any) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    actual = _json_type_name(value)
    return actual == expected


def _json_equal(left: Any, right: Any) -> bool:
    """JSON 语义的值相等：bool 与 number 不算相等（Python 中 True == 1）。"""
    if _json_type_name(left) != _json_type_name(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[k], right[k]) for k in left
        )
    return left == right


def _schema_path(schema_path: str, suffix: str) -> str:
    """拼接模式内定位；根节点显示为以单个 / 开头。"""
    return f"{schema_path}{suffix}" if schema_path else suffix


def validate_event(schema: dict[str, Any], event: Any) -> list[dict[str, str]]:
    """校验单个事件，返回违规列表（空列表表示通过）。"""
    violations: list[dict[str, str]] = []
    _validate_instance(schema, event, "$", "", violations)
    return violations


def validate_events(
    schema: dict[str, Any], events: list[Any]
) -> list[dict[str, Any]]:
    """逐事件校验整批事件，返回带 event_index / json_path 的全部违规。"""
    found: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        for v in validate_event(schema, event):
            found.append({"event_index": index, **v})
    return found


def _validate_instance(
    node: dict[str, Any],
    value: Any,
    path: str,
    schema_path: str,
    violations: list[dict[str, str]],
) -> None:
    # enum 与类型无关，先检查（JSON Schema 中 enum 成功即可独立成立，
    # 但这里要求同时满足其余约束——子集内保持严格语义）。
    if "enum" in node and not any(
        _json_equal(value, option) for option in node["enum"]
    ):
        violations.append(
            {
                "json_path": path,
                "schema_path": _schema_path(schema_path, "/enum"),
                "message": f"值 {value!r} 不在枚举 {node['enum']!r} 中",
            }
        )

    expected = node.get("type")
    if expected is not None and not _type_matches(expected, value):
        violations.append(
            {
                "json_path": path,
                "schema_path": _schema_path(schema_path, "/type"),
                "message": f"类型应为 {expected}，实际为 {_json_type_name(value)}",
            }
        )
        # 类型不符时继续深入只会派生噪声错误
        return

    if isinstance(value, dict):
        _validate_object(node, value, path, schema_path, violations)
    elif isinstance(value, list):
        _validate_array(node, value, path, schema_path, violations)


def _validate_object(
    node: dict[str, Any],
    value: dict[str, Any],
    path: str,
    schema_path: str,
    violations: list[dict[str, str]],
) -> None:
    sp = schema_path or ""
    for name in node.get("required", []):
        if name not in value:
            violations.append(
                {
                    "json_path": f"{path}.{name}",
                    "schema_path": _schema_path(sp, "/required"),
                    "message": f"缺少必填属性 {name!r}",
                }
            )

    props = node.get("properties") or {}
    for name, sub in props.items():
        if name in value:
            _validate_instance(
                sub,
                value[name],
                f"{path}.{name}",
                f"{schema_path}/properties/{name}",
                violations,
            )


def _validate_array(
    node: dict[str, Any],
    value: list[Any],
    path: str,
    schema_path: str,
    violations: list[dict[str, str]],
) -> None:
    item_schema = node.get("items")
    if item_schema is not None:
        for i, element in enumerate(value):
            _validate_instance(
                item_schema,
                element,
                f"{path}[{i}]",
                f"{schema_path}/items",
                violations,
            )
