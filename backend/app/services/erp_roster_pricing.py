"""单日名单正式批次只消费绑定到真实采集的当日 ERP 报告。"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import select, update

from app.adapters.base import SourceUnavailable
from app.adapters.erp_pricing import MappedKey, prepare_pricing
from app.adapters.live_online_preparation import prepare_online_states
from app.domain.engine import REQUIRED_SOURCES
from app.domain.types import Decision, Snapshot
from app.db import transaction
from app.models import Audit, Batch, Job
from app.observability import event
from app.services.erp_capture_store import load_capture, write_verified
from app.services.erp_pricing import _load_pricing_artifact, save_pricing_preview
from app.services.erp_workflow import ERP_ROSTER_DEPARTMENTS, run_workflow
from app.services.store import BEIJING, stamp, utcnow


@dataclass(frozen=True)
class ApprovedRosterPrice:
    upload_price: Decimal
    target_actual: Decimal
    shop_name: str | None
    fulfillment: str | None

    def as_json(self):
        return [str(self.upload_price), str(self.target_actual), self.shop_name, self.fulfillment]


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
    # 人员、部门与来源保持真实执行归属；四类来源不再要求齐备。
    if (raw.get('source_kind') != 'erp_web_backend'
            or seller.get('name') != person or raw.get('department') != department
            or ERP_ROSTER_DEPARTMENTS.get(person) != department or scope.get('mode') != 'capture'
            or report.get('department') != department or report.get('business_date') != day.isoformat()):
        raise ValueError('ERP 名单预览与本轮真实采集范围不一致')
    snapshot = Snapshot.model_validate(report['_snapshot'])
    if (snapshot.business_date != day or snapshot.fx_date != day
            or snapshot.source_versions.get('erp_capture_id') != capture_id
            or not REQUIRED_SOURCES.intersection(snapshot.complete_sources)):
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


def _scoped_collect(initial):
    # 复用首轮当日归档，现价和折扣由上传前单品预检负责。
    return lambda day: initial


def run_roster_pricing(service, run_key, day, scope, *, zero_stock_targets=None):
    name = scope['salesperson']
    if (service.adapter.mode != 'live' or name not in ERP_ROSTER_DEPARTMENTS
            or scope['department'] != ERP_ROSTER_DEPARTMENTS[name]):
        raise ValueError('名单正式批次须绑定真实定价服务和人员部门')
    if not scope['ready_prices']:
        return None
    config = service.config.model_copy(update={'live_submit_enabled': False})
    result = {'salesperson': name, 'state': 'blocked'}
    try:
        snapshot, eligible, prices = _snapshot(config.runtime_dir, day, scope)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result['error_id'] = event('erp.failed', exception=exc, source='erp',
                                   capture_id=scope.get('capture_id'), stage='pricing')
        return result

    if zero_stock_targets is not None:
        zero_stock = {row.key.identity() for row in snapshot.online
                      if row.profit_check and row.profit_check.overseas_stock == 0
                      and row.key.target_identity() in zero_stock_targets}
        eligible = [row for row in eligible if row.key.identity() in zero_stock]
    allowed = {row.key.identity(): prices[row.key.identity()] for row in eligible}
    if not allowed:
        return result

    # 当前销售员的真实来源、网页归属和稳定商品锁随执行再次核对。
    scoped_run_key = f'erp-roster:{run_key}:{name}'
    collect = _scoped_collect((snapshot, allowed))
    batch = RosterBatch(day, name, collect, allowed)
    try:
        batch_id = service.run_batch(scoped_run_key, roster=batch)
    except SourceUnavailable as exc:
        result['error_id'] = event('erp.failed', exception=exc, source='erp', stage='pricing')
        with service.factory() as session:
            batch_id = session.scalar(select(Batch.id).where(
                Batch.run_key == f'{scoped_run_key}:{day.isoformat()}', Batch.mode == 'live'))
        if batch_id:
            result['batch_id'] = batch_id
    else:
        result.update(state='evaluated', batch_id=batch_id)
    return result


def _read_evidence(reference):
    content = Path(reference['artifact_file']).read_bytes()
    return json.loads(content)


def _reprice_archived_scope(runtime, day, scope):
    initial, digest = _load_pricing_artifact(runtime, scope['capture_id'], scope['preview_id'])
    refs = initial['references']
    if not refs.get('online_preparation'):
        return scope, set()
    old_online = _read_evidence(refs['online_preparation'])
    blocked = {
        MappedKey.model_validate(issue['target']).target_identity()
        for issue in old_online['issues']
        if issue['reason'] == '批量商品匹配不唯一或当前仓配与 ERP 不一致'
    }
    if not blocked:
        return scope, set()

    capture = load_capture(runtime, scope['capture_id'])
    order = _read_evidence(refs['order_preparation'])
    sales = _read_evidence(refs['sales_source'])
    platform = _read_evidence(refs['platform_current_state'])
    exported = {}
    for row in sales['variants']:
        key = (row['shopName'], row['productId'], row['sellerSkuId'])
        exported.setdefault(key, []).append(row['batch_pricing']['海外仓库存'])
    current = {}
    for row in platform['records']:
        for variant in row['current_variants']:
            key = (row['erp_shop'], row['product_id'], variant['sellerSkuId'])
            current.setdefault(key, []).append(variant.get('overseasStockSum'))

    def confirmed_zero(values):
        try:
            return len(values) == 1 and Decimal(str(values[0])) == 0
        except InvalidOperation:
            return False

    passed = {MappedKey.model_validate(row['key']).target_identity()
              for row in scope['history_filter'] if row['status'] == 'passed'}
    prepared = prepare_pricing(capture, order_preparation=order).report['references']
    selected, targets = set(), []
    for row_id in prepared['retained_row_ids']:
        row = prepared['lines'][row_id]
        key = MappedKey.model_validate(row['target'])
        if key.target_identity() not in passed or Decimal(row['loss_input']['amount']) >= 0:
            continue
        listing = (row['erp_shop'], key.product_id, key.item_id)
        if (key.target_identity() in blocked and confirmed_zero(exported.get(listing, []))
                and confirmed_zero(current.get(listing, []))):
            selected.add(key.target_identity())
        targets.append({'erp_shop': row['erp_shop'], 'erp_sku': order['proofs'][row['order_id']]['erp_sku'],
                        'main_sku': row['main_sku'], 'key': row['target']})
    if not selected:
        return scope, set()

    fee_ref = refs['fixed_fees']
    fixed_fees = _read_evidence(fee_ref)
    pricing_reference_dir = runtime / 'sku-price-references'
    prices = None
    for path in pricing_reference_dir.glob('*/result.json'):
        observed = json.loads(path.read_bytes())
        if observed.get('erp_capture_id') == capture['capture_id']:
            if prices is None or observed['completed_at'] > prices['completed_at']:
                prices = observed
    online = prepare_online_states(capture, targets, sales=sales, platform=platform,
                                   local_history=_read_evidence(refs['local_price_history']),
                                   fixed_fees=fixed_fees,
                                   official_history=_read_evidence(old_online['references']['official_price_history']),
                                   price_references=prices)
    preview = save_pricing_preview(
        runtime, scope['capture_id'], current_fx=refs['current_fx'],
        sales_source=refs['sales_source'], native_source=refs['native_details'],
        order_preparation=order, order_headers_source=refs['order_headers'],
        local_history_source=refs['local_price_history'], platform_source=refs['platform_current_state'],
        online_preparation=online, fixed_fees_source=fee_ref,
    )
    _, digest = _load_pricing_artifact(runtime, scope['capture_id'], preview['preview_id'])
    return {**scope, 'preview_id': preview['preview_id'], 'preview_sha256': digest,
            'ready_prices': preview['summary']['ready_prices'], 'summary': preview['summary']}, selected


def _stock_resume_manifest(runtime, day, manifest_id):
    if not isinstance(manifest_id, str) or not re.fullmatch(r'[a-f0-9]{32}', manifest_id):
        raise ValueError('续跑必须绑定原名单工作流')
    path = runtime / 'erp-workflows' / manifest_id / 'result.json'
    capture = json.loads(path.read_bytes())
    scopes = capture['departments']
    if (capture.get('roster_date') != day.isoformat() or capture['status'] == 'running'
            or [scope['salesperson'] for scope in scopes] != list(ERP_ROSTER_DEPARTMENTS)):
        raise ValueError('续跑必须绑定同日已结束的完整七人采集记录')
    return path, capture




def requeue_roster_stock_resume(factory, runtime, day, manifest_id):
    if utcnow().astimezone(BEIJING).date() != day:
        raise ValueError('零库存续跑不能跨业务日')
    _stock_resume_manifest(Path(runtime), day, manifest_id)
    with transaction(factory) as session:
        job = session.scalar(select(Job).where(Job.dedupe_key == f'daily:{day.isoformat()}'))
        if not job or job.kind != 'run':
            raise ValueError('原名单任务不存在')
        if job.payload.get('resume_roster_workflow') == manifest_id:
            return job.id
        if job.state not in ('done', 'failed', 'cancelled'):
            raise SourceUnavailable('原名单任务仍在运行或等待重试')
        prior_state, prior_error = job.state, job.error
        payload = {**job.payload, 'resume_roster_workflow': manifest_id}
        changed = session.execute(update(Job).where(Job.id == job.id, Job.state == prior_state).values(
            payload=payload, state='queued', owner=None, lease_until=0, error=None))
        if changed.rowcount != 1:
            raise SourceUnavailable('原名单任务状态已变化')
        session.add(Audit(id=str(uuid4()), action='roster.zero_stock_requeued', actor='admin',
                          detail={'job_id': job.id, 'workflow_id': manifest_id,
                                  'prior_state': prior_state, 'prior_error': prior_error},
                          created_at=stamp()))
        return job.id


def resume_roster_stock_pricing(service, job_id, day, manifest_id):
    runtime = Path(service.config.runtime_dir)
    path, capture = _stock_resume_manifest(runtime, day, manifest_id)
    needs_refresh = False
    resumes = capture.setdefault('zero_stock_resume', {})
    for scope in capture['departments']:
        name = scope['salesperson']
        previous = resumes.get(name, {})
        if 'targets' in previous:
            repriced, selected = previous['scope'], set(previous['targets'])
        elif scope.get('preview_id'):
            repriced, selected = _reprice_archived_scope(runtime, day, scope)
            resumes[name] = {'scope': repriced, 'targets': sorted(selected)}
            write_verified(path, capture)
        else:
            continue
        if not selected or 'pricing_batch' in previous:
            continue
        result = run_roster_pricing(service, f'{job_id}:stock', day, repriced, zero_stock_targets=selected)
        if result and result.get('error_id'):
            needs_refresh = True
            continue
        resumes[name]['pricing_batch'] = result
        write_verified(path, capture)
    if needs_refresh:
        raise SourceUnavailable('仓名补算后仍需重取当前数据')
    return {name: {'targets': len(state['targets']), 'pricing_batch': state.get('pricing_batch')}
            for name, state in resumes.items()}
