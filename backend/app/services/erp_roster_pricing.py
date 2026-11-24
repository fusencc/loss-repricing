"""单日名单正式批次只消费绑定到真实采集的当日 ERP 报告。"""
from collections import defaultdict

from sqlalchemy import select

from app.adapters.base import SourceUnavailable
from app.domain.engine import REQUIRED_SOURCES
from app.domain.types import Decision, Snapshot
from app.models import Batch
from app.observability import event
from app.services.erp_capture_store import load_capture
from app.services.erp_pricing import _load_pricing_artifact
from app.services.erp_workflow import ERP_ROSTER_DEPARTMENTS, run_workflow


def _snapshot(runtime_dir, day, scope):
    capture_id = scope['capture_id']
    person, department = scope['salesperson'], scope['department']
    report, digest = _load_pricing_artifact(runtime_dir, capture_id, scope['preview_id'])
    raw = load_capture(runtime_dir, capture_id)
    seller = raw.get('salesperson') or {}
    # 来源、日期、人员及部门先于金额检查。文件回放、样本和不完整的四类来源不得借名单提交。
    if (digest != scope['preview_sha256'] or raw.get('source_kind') != 'erp_web_backend'
            or raw.get('sample_only') is not False or raw.get('synthetic', False) is not False
            or seller.get('name') != person or raw.get('department') != department
            or ERP_ROSTER_DEPARTMENTS.get(person) != department or scope.get('mode') != 'capture'
            or report.get('synthetic') is not False or report.get('department') != department
            or report.get('business_date') != day.isoformat() or report.get('consistent_capture_scope') is not True):
        raise ValueError('ERP 名单预览与本轮真实采集范围不一致')
    snapshot = Snapshot.model_validate(report['_snapshot'])
    if (snapshot.business_date != day or snapshot.fx_date != day
            or snapshot.source_versions.get('erp_capture_id') != capture_id
            or set(snapshot.complete_sources) != REQUIRED_SOURCES):
        raise ValueError('ERP 名单快照日期、来源或四类覆盖未通过')
    ready = [Decision.model_validate(row) for row in report['decisions'] if row['ready']]
    snapshot.source_versions.update(
        erp_roster_preview=scope['preview_id'], erp_roster_preview_sha256=digest,
        erp_roster_salesperson=person, erp_roster_department=department,
    )
    return snapshot, ready


def _scoped_collect(config, name, initial):
    def collect(day):
        nonlocal initial
        if initial is not None:
            snapshot, initial = initial, None
        else:
            # 提交前沿用同一人的四类来源重取；不能使用其他人的数据或昨日归档。
            try:
                fresh = run_workflow(config, roster_date=day, roster_person=name)
                scope, = fresh['departments']
                if scope['salesperson'] != name:
                    raise ValueError('刷新销售员与审批范围不一致')
                snapshot, _ = _snapshot(config.runtime_dir, day, scope)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SourceUnavailable('名单范围提交前重取未完成') from exc
        # 规则引擎需要完整快照辨认同订单或子 SKU 的关联冲突，授权仅筛选算价输出。
        return snapshot
    return collect


def run_roster_pricing(service, run_key, day, capture):
    if service.adapter.mode != 'live' or capture.get('roster_date') != day.isoformat():
        raise ValueError('名单正式批次须使用同日真实定价服务')
    scopes = capture['departments']
    names = [scope['salesperson'] for scope in scopes]
    if len(names) != len(ERP_ROSTER_DEPARTMENTS) or set(names) != ERP_ROSTER_DEPARTMENTS.keys():
        raise ValueError('名单正式批次缺少完整的人员范围记录')
    config = service.config.model_copy(update={'live_submit_enabled': False})
    prepared, results = [], []
    owners = defaultdict(set)
    for scope in scopes:
        if not scope.get('preview_id'):
            continue
        name = scope['salesperson']
        try:
            snapshot, eligible = _snapshot(config.runtime_dir, day, scope)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if scope['ready_prices']:
                results.append({'salesperson': name, 'state': 'blocked',
                                'error_id': event('erp.failed', exception=exc, source='erp',
                                                  capture_id=scope.get('capture_id'), stage='pricing')})
            continue
        for row in (*snapshot.lines, *snapshot.online, *snapshot.source_issues):
            owners[row.key.target_identity()].add(name)
        if eligible:
            prepared.append((name, snapshot, eligible))

    for name, snapshot, eligible in prepared:
        # 同一上传目标出现于多个销售员的有效快照时，所有关联范围都挂起。
        allowed = {row.key.identity(): (row.upload_price, row.target_actual)
                   for row in eligible if owners[row.key.target_identity()] == {name}}
        result = {'salesperson': name, 'state': 'blocked', 'conflicts': len(eligible) - len(allowed)}
        if allowed:
            scoped_run_key = f'erp-roster:{run_key}:{name}'
            try:
                batch_id = service.run_batch(
                    scoped_run_key, scoped_collect=_scoped_collect(config, name, snapshot),
                    fixed_day=day, approved_prices=allowed,
                )
            except SourceUnavailable as exc:
                result['error_id'] = event('erp.failed', exception=exc, source='erp', stage='pricing')
                # 刷新失败时前面独立商品可能已经提交，保留批次链接供后续核验和通知。
                with service.factory() as session:
                    batch_id = session.scalar(select(Batch.id).where(
                        Batch.run_key == f'{scoped_run_key}:{day.isoformat()}', Batch.mode == 'live'))
                if batch_id:
                    result['batch_id'] = batch_id
            else:
                result.update(state='evaluated', batch_id=batch_id)
        results.append(result)
    return results
