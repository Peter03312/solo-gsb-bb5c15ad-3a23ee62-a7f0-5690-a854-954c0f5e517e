"""确定性哈希与 Merkle 树工具。

所有哈希均为 SHA-256，hex 编码，并通过域分隔前缀避免不同结构之间碰撞：

* 事件叶子:   sha256(b"\x00EVT" + canonical_json(event))
* Merkle 内部: sha256(b"\x01NODE" + left_hash_bytes + right_hash_bytes)
* 批次哈希:   sha256(b"\x00BATCH" + canonical_json({...批次元数据...}))
* 封存根:     以各批次哈希为叶子的 Merkle 树根
* 空串锚点:   sha256(b"") —— 创世批次(batch_no=1)的 prev_hash
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 创世批次（batch_no == 1）必须指向的 prev_hash
GENESIS_HASH = hashlib.sha256(b"").hexdigest()

_LEAF_PREFIX = b"\x00EVT"
_BATCH_PREFIX = b"\x00BATCH"
_NODE_PREFIX = b"\x01NODE"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """生成确定形式的 JSON 字节串：UTF-8、无空格、键排序、不转义非 ASCII。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def event_leaf(event: Any) -> str:
    """单个事件的叶子哈希（对规范化后的事件内容计算）。"""
    return sha256_hex(_LEAF_PREFIX + canonical_json(event))


def hash_pair(left: str, right: str) -> str:
    """Merkle 内部节点哈希（顺序敏感，left/right 不可交换）。"""
    return sha256_hex(_NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right))


def merkle_root(leaves: list[str]) -> str:
    """由叶子列表计算 Merkle 根；空列表返回空串，单叶子返回其自身。"""
    if not leaves:
        return ""
    level = list(leaves)
    while len(level) > 1:
        nxt: list[str] = []
        # 奇数节点：复制最后一个节点
        if len(level) % 2 == 1:
            level.append(level[-1])
        for i in range(0, len(level), 2):
            nxt.append(hash_pair(level[i], level[i + 1]))
        level = nxt
    return level[0]


def merkle_proof(leaves: list[str], index: int) -> list[dict[str, str]]:
    """生成叶子 index 的包含证明，路径项形如 {"side":"left"/"right","hash":...}。

    side 表示兄弟节点在拼接时的位置；单叶子树返回空路径。
    """
    if not leaves:
        raise ValueError("empty leaves")
    if index < 0 or index >= len(leaves):
        raise IndexError("leaf index out of range")

    proof: list[dict[str, str]] = []
    level = list(leaves)
    idx = index
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        if idx % 2 == 0:
            proof.append({"side": "right", "hash": level[idx + 1]})
        else:
            proof.append({"side": "left", "hash": level[idx - 1]})
        nxt: list[str] = []
        for i in range(0, len(level), 2):
            nxt.append(hash_pair(level[i], level[i + 1]))
        level = nxt
        idx //= 2
    return proof


def verify_merkle_proof(leaf: str, proof: list[dict[str, str]], root: str) -> bool:
    """根据包含证明重算根并比对。"""
    node = leaf
    for step in proof:
        sibling = step["hash"]
        if step["side"] == "left":
            node = hash_pair(sibling, node)
        elif step["side"] == "right":
            node = hash_pair(node, sibling)
        else:
            return False
    return node == root


def batch_hash(source_id: str, batch_no: int, prev_hash: str, events: list[Any]) -> str:
    """批次哈希：对 (source_id, batch_no, prev_hash, 事件 Merkle 根) 规范化后哈希。

    批次自身即包含其事件集合的 Merkle 根，因此封存根可由各批次哈希独立复算。
    """
    leaves = [event_leaf(e) for e in events]
    payload = {
        "source_id": source_id,
        "batch_no": batch_no,
        "prev_hash": prev_hash,
        "events_root": merkle_root(leaves),
    }
    return sha256_hex(_BATCH_PREFIX + canonical_json(payload))


def bind_seal_root(
    source_id: str, from_batch_no: int, to_batch_no: int, tree_root: str
) -> str:
    """将批次树根与来源 / 封存区间绑定，得到最终封存根。"""
    payload = {
        "source_id": source_id,
        "from_batch_no": from_batch_no,
        "to_batch_no": to_batch_no,
        "tree": "merkle-sha256",
        "batch_tree_root": tree_root,
    }
    return sha256_hex(b"\x00SEAL" + canonical_json(payload))


def seal_roots(
    source_id: str,
    from_batch_no: int,
    to_batch_no: int,
    batch_hashes: list[str],
) -> tuple[str, str]:
    """计算封存相关两个根，返回 (batch_tree_root, seal_root)。

    * batch_tree_root —— 按 batch_no 顺序排列的各批次哈希构成的 Merkle 树根，
      批次包含证明对此根验证；
    * seal_root —— 将 batch_tree_root 与 source_id / 封存区间绑定后再哈希，
      防止跨来源、跨区间的重放。
    """
    tree_root = merkle_root(batch_hashes)
    sealed = bind_seal_root(source_id, from_batch_no, to_batch_no, tree_root)
    return tree_root, sealed
