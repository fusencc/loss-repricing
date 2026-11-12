"""管理员日志接口；导出仅在完整有界扫描后返回，并持久化审计。"""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from app.api.auth import require_admin
from app.db import transaction
from app.models import Audit
from app.observability import event, get_sink
from app.services.log_reader import FILTER_KEYS, LogReader, LogReadError
from app.services.store import stamp

router = APIRouter(prefix="/api/logs", tags=["logs"], dependencies=[Depends(require_admin)])


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str | None = None
    end: str | None = None
    role: str | None = None
    level: str | None = None
    event: str | None = None
    error_id: str | None = None
    request_id: str | None = None
    job_id: str | None = None
    batch_id: str | None = None
    execution_id: str | None = None
    capture_id: str | None = None
    item_id: str | None = None
    operation_id: str | None = None


def invoke(function, **kwargs):
    try:
        return function(**kwargs)
    except LogReadError as exc:
        raise HTTPException(exc.status, exc.code) from None


def check_query(request, extra):
    if set(request.query_params) - FILTER_KEYS - set(extra):
        raise HTTPException(422, "invalid_log_filter")
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(422, "duplicate_log_filter")


@router.get("/status")
def status(request: Request):
    sink = getattr(request.app.state, "log_sink", None)
    if sink is None:
        sink = get_sink(request.app.state.config)
    # 状态由核心返回脱敏白名单，不返回root或运行目录。
    return sink.status()


@router.get("")
def logs(request: Request, start: str | None = None, end: str | None = None,
         limit: int = Query(default=100, ge=1, le=500), cursor: str | None = None):
    check_query(request, {"start", "end", "limit", "cursor"})
    filters = {key: request.query_params[key] for key in FILTER_KEYS if key in request.query_params}
    return invoke(LogReader(request.app.state.config).query, filters=filters, start=start, end=end, limit=limit, cursor=cursor)


@router.get("/errors/{error_id}")
def error_context(error_id: str, request: Request, start: str | None = None, end: str | None = None,
                  limit: int = Query(default=100, ge=1, le=500)):
    if set(request.query_params) - {"start", "end", "limit"} or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(422, "invalid_log_filter")
    return invoke(LogReader(request.app.state.config).error_context, error_id=error_id, start=start, end=end, limit=limit)


@router.post("/export")
def export_logs(body: ExportRequest, request: Request, actor: str = Depends(require_admin)):
    values = body.model_dump(exclude_none=True)
    start, end = values.pop("start", None), values.pop("end", None)
    output, count, detail = invoke(LogReader(request.app.state.config).query, filters=values, start=start, end=end, export=True)
    # 仅完成且大小受限的脱敏JSONL可下载；审计提交失败则不返回文件，避免未留证导出。
    with transaction(request.app.state.factory) as session:
        session.add(Audit(id=str(uuid4()), action="logs.export", actor=actor,
                          detail={**detail, "count": count, "bytes": len(output)}, created_at=stamp()))
    event("logs.exported", count=count)
    return Response(output, media_type="application/x-ndjson", headers={
        "Content-Disposition": 'attachment; filename="logs.jsonl"', "Cache-Control": "no-store"})
