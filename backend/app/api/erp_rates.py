"""管理员ERP采集与文件算价入口；独立于实时执行。

POST /captures：四类批量导出并保存，可指定单一来源。
GET /sample、/validation：只读四组第一页，用于管理员诊断。
GET /rates：独立/open汇率查询，不与网页令牌混用，结果不注入定价快照。
GET /captures/{capture_id}：仅查询已保存文件，不重新请求ERP。
POST /captures/{capture_id}/pricing-previews：使用已保存批量采集做只读算价。
这些入口可主动访问真实ERP（历史文件读取除外），均需管理员权限。
"""
from typing import Annotated, Literal
from functools import wraps, partial
from uuid import uuid4
from time import monotonic
from app.observability import bind_context, event, valid_id
from app.adapters.erp_bulk_capture import capture_erp_bulk
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, ValidationError
from app.adapters.erp_capture import capture_erp_sample
from app.services.erp_capture_store import save_capture, load_capture, find_saved_order
from app.adapters.base import SourceUnavailable
from app.adapters.erp_rates import query_erp_rates
from app.adapters.erp_pricing import PricingEvidence
from app.services.erp_pricing import (
    find_preview_reconciliation, load_pricing_preview, reconciliation_key, save_pricing_preview,
)
from app.services.store import enqueue
from app.api.auth import require_admin

# 来源标签显式传给事件；bind_context仅接受安全关联编号。
event = partial(event, source='erp', synthetic=False)

router = APIRouter(prefix='/api/sources/erp', dependencies=[Depends(require_admin)])


def _record_failure(request, error, *, stage, http_status, error_code):
    """转换HTTP响应前保存原异常定位；同一请求只使用已有或新生成的错误编号。"""
    error_id = getattr(request.state, 'error_id', None)
    if not valid_id(error_id):
        # 保存层可能已记录原异常；只复用本项目生成且通过安全格式校验的编号。
        error_id = getattr(error, '_erp_log_error_id', None)
    if not valid_id(error_id):
        error_id = event('erp.failed', exception=error, stage=stage,
                         http_status=http_status, error_code=error_code)
    if error_id:
        request.state.error_id = error_id
    return error_id


def _erp_operation(stage):
    """管理员来源入口访问真实ERP；日志独立标记来源。"""
    def decorate(function):
        @wraps(function)
        def wrapped(request: Request, *args, **kwargs):
            with bind_context(operation_id=uuid4().hex):
                started = monotonic()
                event('erp.started', stage=stage)
                try:
                    result = function(request, *args, **kwargs)
                except HTTPException as error:
                    error_id = _record_failure(request, error, stage=stage,
                                               http_status=error.status_code, error_code='http_error')
                    if error_id:
                        error.headers = dict(error.headers or {}, **{'X-Error-ID': error_id})
                    raise
                except Exception as error:
                    _record_failure(request, error, stage=stage, http_status=500, error_code='http_error')
                    raise
                event('erp.completed', stage=stage,
                      duration_ms=round((monotonic() - started) * 1000))
                return result
        return wrapped
    return decorate


@router.get('/sample')
@_erp_operation('collect')
def sample(request: Request, limit: int = Query(default=5, ge=1, le=10)):
    """管理员主动只读抽样；不调用调价服务，不覆盖正式定价快照。"""
    try:
        return capture_erp_sample(request.app.state.config, limit=limit)
    except SourceUnavailable as error:
        _record_failure(request, error, stage='collect', http_status=503, error_code='source_unavailable')
        raise HTTPException(503, str(error)) from None


@router.get('/validation')
def validation(request: Request, limit: int = Query(default=2, ge=1, le=10)):
    """只返回诊断报告，不下发原始订单明细；必须由管理员主动请求。"""
    return sample(request, limit)['validation']


@router.get('/rates')
@_erp_operation('rates')
def rates(request: Request):
    try:
        return query_erp_rates(request.app.state.config)
    except SourceUnavailable as error:
        _record_failure(request, error, stage='rates', http_status=503, error_code='source_unavailable')
        raise HTTPException(503, str(error)) from None


class CaptureQuery(BaseModel):
    # 未知参数必须在读取前拒绝，不能将原本有界的请求误当作全量采集。
    model_config = ConfigDict(extra='forbid')
    source: Literal['loss_allocatable', 'loss_pending', 'blocked_allocatable', 'blocked_pending'] | None = None


@router.post('/captures')
@_erp_operation('collect')
def capture(request: Request, query: Annotated[CaptureQuery, Query()]):
    try:
        settings = request.app.state.config
        result = capture_erp_bulk(settings, source=query.source, event_callback=event)
        saved = save_capture(settings.runtime_dir, result)
        response = {key: saved[key] for key in ('capture_id', 'storage_verified', 'queried_at',
                                                'capture_complete', 'pricing_ready', 'summary')}
        if 'cleaning_status' in saved:
            response['cleaning_status'] = saved['cleaning_status']
        response['stopped_by_rate_limit'] = saved.get('stopped_by_rate_limit', False)
        response['not_read_groups'] = saved.get('not_read_groups', [])
        return response
    except SourceUnavailable as error:
        _record_failure(request, error, stage='collect', http_status=503, error_code='source_unavailable')
        raise HTTPException(503, str(error)) from None
    except (OSError, ValueError) as error:
        _record_failure(request, error, stage='save', http_status=503, error_code='storage_failed')
        raise HTTPException(503, 'ERP采集文件未能确认保存，请核实服务运行目录') from None


@router.get('/captures/{capture_id}')
@_erp_operation('read')
def read_capture(request: Request, capture_id: str, orderid: Annotated[str | None, Query()] = None):
    """读取已保存结果，可按订单号过滤；不触发ERP请求。"""
    try:
        runtime_dir = request.app.state.config.runtime_dir
        if orderid is not None:
            return find_saved_order(runtime_dir, capture_id, orderid)
        return load_capture(runtime_dir, capture_id)
    except (FileNotFoundError, KeyError) as error:
        _record_failure(request, error, stage='read', http_status=404, error_code='storage_failed')
        raise HTTPException(404, '未找到采集文件或本轮订单') from None
    except ValueError as error:
        _record_failure(request, error, stage='read', http_status=422, error_code='invalid_data')
        raise HTTPException(422, '采集编号或文件内容无效') from None
    except OSError as error:
        _record_failure(request, error, stage='read', http_status=503, error_code='storage_failed')
        raise HTTPException(503, '采集文件读取失败') from None



def _save_preview(request, capture_id, evidence, *, exports=None):
    try:
        return save_pricing_preview(request.app.state.config.runtime_dir, capture_id, evidence, exports=exports)
    except FileNotFoundError:
        raise HTTPException(404, '未找到指定的ERP批量采集文件') from None
    except ValidationError:
        raise HTTPException(422, '采集文件不符合当前批量格式') from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(503, '采集文件读取或算价报告保存失败') from None


@router.post('/captures/{capture_id}/pricing-previews', status_code=201)
def create_pricing_preview(request: Request, capture_id: str, evidence: PricingEvidence | None = None):
    return _save_preview(request, capture_id, evidence)


@router.post('/captures/{capture_id}/pricing-previews/from-exports', status_code=201)
def create_pricing_preview_from_exports(
    request: Request, capture_id: str, erp_details: UploadFile = File(), sales_variants: UploadFile = File(),
    evidence_json: UploadFile | None = File(default=None),
):
    # 与部署的 nginx 5m 限制一致。正文包含两个导出、可选补充证据和 multipart 封装。
    remaining = 5 * 1024 * 1024
    body_length = request.headers.get('content-length', '')
    limit_error = '上传请求须在 5 MiB 内（含导出、补充证据及表单）'
    if body_length.isascii() and body_length.isdecimal() and (
        len(body_length) > 10 or int(body_length) > remaining
    ):
        raise HTTPException(413, limit_error)

    uploads = [erp_details, sales_variants]
    if evidence_json is not None:
        uploads.append(evidence_json)
    contents = []
    try:
        for source in uploads:
            data = source.file.read(remaining + 1)
            remaining -= len(data)
            if remaining < 0:
                raise HTTPException(413, limit_error)
            contents.append(data)
    except OSError:
        raise HTTPException(422, '导出文件读取失败') from None

    try:
        evidence = PricingEvidence.model_validate_json(contents[2]) if evidence_json is not None else None
    except ValidationError:
        raise HTTPException(422, '补充证据不符合当前 JSON 格式') from None
    return _save_preview(request, capture_id, evidence, exports=(contents[0], contents[1]))


def _read_saved_preview(state, capture_id, preview_id):
    try:
        report, digest = load_pricing_preview(state.config.runtime_dir, capture_id, preview_id)
    except FileNotFoundError:
        raise HTTPException(404, '未找到这次采集的算价报告') from None
    except ValueError:
        raise HTTPException(422, '采集编号、报告编号或文件内容无效') from None
    except OSError:
        raise HTTPException(503, '算价报告读取失败') from None
    if state.config.adapter_mode == 'file':
        with state.factory() as session:
            try:
                batch_id = find_preview_reconciliation(session, reconciliation_key(capture_id, preview_id), digest)
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
        if batch_id:
            report['reconciliation_batch_id'] = batch_id
    return report, digest


@router.get('/captures/{capture_id}/pricing-previews/{preview_id}')
def read_pricing_preview(request: Request, capture_id: str, preview_id: str):
    report, _ = _read_saved_preview(request.app.state, capture_id, preview_id)
    return report


@router.post('/captures/{capture_id}/pricing-previews/{preview_id}/reconciliation', status_code=202)
def reconcile_pricing_preview(request: Request, capture_id: str, preview_id: str):
    state = request.app.state
    if state.config.adapter_mode != 'file':
        raise HTTPException(409, 'ERP 报告只能在文件对账环境入批')
    report, digest = _read_saved_preview(state, capture_id, preview_id)
    if not report['batch_input_available']:
        raise HTTPException(409, '这份报告没有可入批的规范快照，请补齐证据后生成新报告')
    job_id = enqueue(state.factory, 'erp_reconciliation', {
        'capture_id': capture_id, 'preview_id': preview_id, 'artifact_sha256': digest,
    }, reconciliation_key(capture_id, preview_id), retry_failed=True)
    return {'job_id': job_id}
