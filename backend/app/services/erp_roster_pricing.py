"""单日名单正式批次只消费绑定到真实采集的当日 ERP 报告。"""
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.adapters.base import SourceUnavailable
from app.domain.engine import REQUIRED_SOURCES
from app.domain.types import Decision, Snapshot
from app.models import Batch
from app.observability import event
from app.services.erp_capture_store import load_capture
from app.services.erp_pricing import _load_pricing_artifact
from app.services.erp_workflow import ERP_ROSTER_DEPARTMENTS, run_workflow


@dataclass(frozen=True)
class ApprovedRosterPrice:
    upload_price: Decimal
    target_actual: Decimal
    shop_name: str | None
    fulfillment: str | None

    def as_json(self):
        return [str(self.upload_price), str(self.target_actual), self.shop_name, self.fulfillment]

    def matches_saved(self, stored):
        if not isinstance(stored, list) or len(stored) != 4:
            return False
        try:
            amounts = tuple(map(Decimal, stored[:2]))
        except (InvalidOperation, TypeError, ValueError):
            return False
        return (self.upload_price, self.target_actual) == amounts and [self.shop_name, self.fulfillment] == stored[2:]


@dataclass(frozen=True)
class RosterBatch:
    day: date
    salesperson: str
    collect: Callable[[date], tuple[Snapshot, dict[str, ApprovedRosterPrice]]]
    prices: dict[str, ApprovedRosterPrice]


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
    # ERP 行引用已绑定商品复合键；店铺展示文本不得自行从 ID、名称或负责人猜测。
    references = report.get('references', {}).get('lines', {})
    prices = {}
    for decision in ready:
        key = decision.key.model_dump(mode='json')
        proof = references.get(decision.selected_row_id, {})
        rows = decision.trace.get('online_rows', [])
        current = rows[0] if len(rows) == 1 and rows[0].get('key') == key else {}
        shop = proof.get('erp_shop')
        if (proof.get('target') != key or proof.get('main_sku') != current.get('main_sku')
                or not isinstance(shop, str) or not shop.strip()):
            shop = None
        if decision.upload_price is None or decision.target_actual is None:
            raise ValueError('算价通过记录缺少精确上传金额')
        prices[decision.key.identity()] = ApprovedRosterPrice(
            decision.upload_price, decision.target_actual, shop, (current.get('profit_check') or {}).get('mode'),
        )
    return snapshot, ready, prices


def _scoped_collect(config, name, initial):
    def collect(day):
        nonlocal initial
        if initial is not None:
            collected, initial = initial, None
            return collected
        else:
            # 提交前沿用同一人的四类来源重取；不能使用其他人的数据或昨日归档。
            try:
                fresh = run_workflow(config, roster_date=day, roster_person=name)
                scope, = fresh['departments']
                if scope['salesperson'] != name:
                    raise ValueError('刷新销售员与审批范围不一致')
                snapshot, _, prices = _snapshot(config.runtime_dir, day, scope)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SourceUnavailable('名单范围提交前重取未完成') from exc
        # 规则引擎需要完整快照辨认同订单或子 SKU 的关联冲突，授权仅筛选算价输出。
        return snapshot, prices
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
            snapshot, eligible, prices = _snapshot(config.runtime_dir, day, scope)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if scope['ready_prices']:
                results.append({'salesperson': name, 'state': 'blocked',
                                'error_id': event('erp.failed', exception=exc, source='erp',
                                                  capture_id=scope.get('capture_id'), stage='pricing')})
            continue
        for row in (*snapshot.lines, *snapshot.online, *snapshot.source_issues):
            owners[row.key.target_identity()].add(name)
        if eligible:
            prepared.append((name, snapshot, eligible, prices))

    for name, snapshot, eligible, prices in prepared:
        # 同一上传目标出现于多个销售员的有效快照时，所有关联范围都挂起。
        allowed = {row.key.identity(): prices[row.key.identity()]
                   for row in eligible if owners[row.key.target_identity()] == {name}}
        result = {'salesperson': name, 'state': 'blocked', 'conflicts': len(eligible) - len(allowed)}
        if allowed:
            scoped_run_key = f'erp-roster:{run_key}:{name}'
            try:
                batch = RosterBatch(day, name, _scoped_collect(config, name, (snapshot, prices)), allowed)
                batch_id = service.run_batch(scoped_run_key, roster=batch)
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
