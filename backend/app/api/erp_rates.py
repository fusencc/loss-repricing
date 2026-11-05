"""管理员主动查询公司汇率，不依赖demo/live调价适配器的就绪状态。"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from app.adapters.erp_capture import capture_erp_sample
from app.adapters.base import SourceUnavailable
from app.adapters.erp_rates import query_erp_rates
from app.api.auth import require_admin

router = APIRouter(prefix='/api/sources/erp', dependencies=[Depends(require_admin)])


@router.get('/sample')
def sample(request: Request, limit: int = Query(default=5, ge=1, le=10)):
    """管理员主动只读抽样；不调用调价服务，不覆盖正式定价快照。"""
    try:
        return capture_erp_sample(request.app.state.config, limit=limit)
    except SourceUnavailable as error:
        raise HTTPException(503, str(error)) from None


@router.get('/rates')
def rates(request: Request):
    try:
        return query_erp_rates(request.app.state.config)
    except SourceUnavailable as error:
        raise HTTPException(503, str(error)) from None
