"""管理员主动查询公司汇率，不依赖demo/live调价适配器的就绪状态。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from app.adapters.base import SourceUnavailable
from app.adapters.erp_rates import query_erp_rates
from app.api.auth import require_admin

router = APIRouter(prefix='/api/sources/erp', dependencies=[Depends(require_admin)])


@router.get('/rates')
def rates(request: Request):
    try:
        return query_erp_rates(request.app.state.config)
    except SourceUnavailable as error:
        raise HTTPException(503, str(error)) from None
