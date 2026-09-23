"""FastAPI 入口：证据批次封存 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import db, service
from .errors import APIError
from .hashing import GENESIS_HASH
from .schemas import SealRequest, SubmitBatchRequest, VerifyRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    app.state.db = conn
    try:
        yield
    finally:
        conn.close()


app = FastAPI(
    title="证据批次封存 API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "多个采集端上传带 source_id / batch_no / prev_hash 的 JSON 事件批次。"
        "服务端暂存乱序批次、幂等去重，仅在哈希链连续闭合后封存，"
        "封存产生确定性 Merkle 根并可返回包含证明。"
    ),
)


def get_conn(request: Request):
    return request.app.state.db


@app.exception_handler(APIError)
async def _api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    # 丢弃失败请求可能遗留的未完成语句/事务，避免连接复用期间脏读
    request.app.state.db.rollback()
    body = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/api/v1/genesis", tags=["meta"])
def genesis():
    """创世锚点：sha256("")，batch_no=1 的 prev_hash 必须为此值。"""
    return {"genesis_prev_hash": GENESIS_HASH}


# ---------------------------------------------------------------- 提交

@app.post("/api/v1/batches", tags=["batches"])
def submit_batch(req: SubmitBatchRequest, response: Response, conn=Depends(get_conn)):
    """提交（或幂等重试）一个事件批次；允许乱序到达。

    新批次返回 201；内容完全一致的重复提交返回 200 且 ``duplicate=true``。
    """
    result = service.submit_batch(
        conn,
        source_id=req.source_id,
        batch_no=req.batch_no,
        prev_hash=req.prev_hash,
        events=req.events,
    )
    response.status_code = 200 if result.get("duplicate") else 201
    return result


# ---------------------------------------------------------------- 查询

@app.get("/api/v1/sources", tags=["query"])
def list_sources(conn=Depends(get_conn)):
    """列出所有来源及其封存水位 / 缺口。"""
    return {"sources": service.list_statuses(conn)}


@app.get("/api/v1/sources/{source_id}/gaps", tags=["query"])
def get_gaps(source_id: str, conn=Depends(get_conn)):
    """查询某来源暂存区缺哪些批次号。"""
    return service.get_gaps(conn, source_id)


@app.get("/api/v1/sources/{source_id}/seal", tags=["query"])
def get_seal(source_id: str, conn=Depends(get_conn)):
    """查询某来源当前封存状态与封存根。"""
    return service.get_seal_status(conn, source_id)


@app.get("/api/v1/batches/{source_id}/{batch_no}", tags=["query"])
def get_batch(source_id: str, batch_no: int, conn=Depends(get_conn)):
    """获取单个批次的元数据与事件原文。"""
    return service.get_batch_detail(conn, source_id, batch_no)


# ---------------------------------------------------------------- 封存

@app.post("/api/v1/seals", tags=["seal"])
def seal_batches(req: SealRequest, conn=Depends(get_conn)):
    """封存连续闭合的哈希链前缀，返回确定性封存 Merkle 根。"""
    return service.seal(conn, req.source_id, req.to_batch_no)


# ---------------------------------------------------------------- 证明 / 校验

@app.get(
    "/api/v1/batches/{source_id}/{batch_no}/proof",
    tags=["verify"],
)
def get_proof(
    source_id: str,
    batch_no: int,
    event_index: int,
    conn=Depends(get_conn),
):
    """获取某封存批次中指定事件的双层 Merkle 包含证明（事件→批次→封存根）。"""
    return service.get_proof(conn, source_id, batch_no, event_index)


@app.post("/api/v1/verify", tags=["verify"])
def verify_proof(req: VerifyRequest, conn=Depends(get_conn)):
    """纯密码学复核一份包含证明；可选与服务端封存锚点比对。"""
    return service.verify_proof(
        conn if req.check_server_anchor else None,
        source_id=req.source_id,
        batch_no=req.batch_no,
        prev_hash=req.prev_hash,
        event=req.event,
        event_index=req.event_index,
        event_proof=req.event_proof,
        events_root=req.events_root,
        batch_proof=req.batch_proof,
        seal_from=req.seal_from,
        seal_to=req.seal_to,
        batch_tree_root=req.batch_tree_root,
        seal_root_value=req.seal_root,
    )
