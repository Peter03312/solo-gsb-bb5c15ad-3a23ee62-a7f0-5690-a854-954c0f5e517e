"""请求 / 响应 Pydantic 模型。

事件本体允许任意 JSON（对象、数组、标量），其内容哈希由规范化 JSON 确定。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class SubmitBatchRequest(BaseModel):
    source_id: str = Field(..., min_length=1, description="采集端 / 证据来源标识")
    batch_no: int = Field(..., ge=1, description="该来源下从 1 开始的单调递增批次号")
    prev_hash: str = Field(..., description="前一批次的 batch_hash；创世批次为 sha256('')")
    events: list[Any] = Field(..., min_length=1, description="本批证据事件（任意 JSON，非空）")


class SealRequest(BaseModel):
    source_id: str = Field(..., min_length=1)
    to_batch_no: Optional[int] = Field(
        None, ge=1, description="封存到指定批次；省略则封存最长连续闭合前缀"
    )


class VerifyRequest(BaseModel):
    source_id: str
    batch_no: int = Field(..., ge=1)
    prev_hash: str
    event: Any
    event_index: int = Field(..., ge=0)
    event_proof: list[dict[str, str]]
    events_root: str
    batch_proof: list[dict[str, str]]
    seal_from: int = Field(..., ge=1)
    seal_to: int = Field(..., ge=1)
    batch_tree_root: str = Field(..., description="封存批次树的 Merkle 根（批次证明验证目标）")
    seal_root: str = Field(..., description="绑定来源与区间后的最终封存根")
    check_server_anchor: bool = Field(
        True, description="是否同时与服务端当前封存锚点比对"
    )
