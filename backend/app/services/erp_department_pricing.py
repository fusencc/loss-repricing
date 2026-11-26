"""将本轮 ERP 部门预览转为正式执行使用的商品、金额和销售员归属。"""
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from app.domain.types import Decision, Snapshot
from app.services.erp_pricing import _load_pricing_artifact


@dataclass(frozen=True)
class DepartmentPrice:
    upload_price: Decimal
    target_actual: Decimal
    shop_name: str
    fulfillment: str
    salesperson: str | None


@dataclass
class DepartmentPriceBatch:
    day: date
    department: str
    snapshot: Snapshot
    decisions: list[Decision]
    prices: dict[str, DepartmentPrice]


def _seller_by_order(runtime_dir, scope):
    if not scope.get('seller_candidates_file'):
        return {}
    workflow_id = Path(scope['artifact_dir']).name
    path = Path(runtime_dir) / 'erp-workflows' / workflow_id / 'seller-candidates.json'
    candidates = json.loads(path.read_bytes())
    return {
        (order['shop_id'], order['order_id']): person
        for person, orders in candidates['sellers'].items() for order in orders
    }


def load_department_batch(runtime_dir: Path, day: date, scope: dict) -> DepartmentPriceBatch | None:
    """保留部门内各条结果，按本轮订单证据和在线子商品绑定可提交价格。"""
    if scope.get('mode') != 'capture' or not scope.get('preview_id'):
        return None
    report, _ = _load_pricing_artifact(runtime_dir, scope['capture_id'], scope['preview_id'])
    if report['synthetic'] or not report.get('_snapshot'):
        return None
    snapshot = Snapshot.model_validate(report['_snapshot'])
    department = scope['department']
    if snapshot.business_date != day or snapshot.fx_date != day or report.get('department') != department:
        return None
    snapshot.source_versions.update(erp_department=department, erp_department_preview=scope['preview_id'])
    decisions = [Decision.model_validate(row) for row in report['decisions']]
    target_counts = Counter(d.key.target_identity() for d in decisions)
    sellers = _seller_by_order(runtime_dir, scope)
    references = report['references']['lines']
    prices = {}
    for decision in decisions:
        key = decision.key.model_dump(mode='json')
        source_rows = [references[row_id] for row_id in decision.source_row_ids if row_id in references]
        matched_rows = [row for row in source_rows if row.get('target') == key]
        owners = {sellers.get((decision.key.shop_id, row['order_id'])) for row in matched_rows}
        person = owners.pop() if len(owners) == 1 else None
        shops = {row.get('erp_shop') for row in matched_rows}
        shop = shops.pop() if len(shops) == 1 else None
        decision.trace['erp_department'] = {'department': department, 'salesperson': person, 'shop_name': shop}
        if not decision.ready:
            continue

        selected = references.get(decision.selected_row_id, {})
        online = decision.trace.get('online_rows', [])
        if (target_counts[decision.key.target_identity()] != 1 or not shop
                or selected.get('target') != key or selected.get('erp_shop') != shop
                or len(online) != 1 or online[0].get('key') != key
                or selected.get('main_sku') != online[0].get('main_sku')):
            reason = '无法将 ERP 订单和唯一在线子商品绑定到同一店铺及 SKU'
        elif (decision.upload_price is None or decision.upload_price <= 0 or
              decision.target_actual is None or decision.target_actual <= 0):
            reason = '本次算价未提供正数上传金额和目标成交价'
        else:
            fulfillment = (online[0].get('profit_check') or {}).get('mode')
            if fulfillment in ('direct', 'overseas'):
                prices[decision.key.identity()] = DepartmentPrice(
                    decision.upload_price, decision.target_actual, shop, fulfillment, person,
                )
                continue
            reason = '在线子商品未提供可确认的仓配方式'
        decision.ready = False
        decision.status = '待处理'
        decision.reason_code = 'department_target_unverified'
        decision.reason = reason
    return DepartmentPriceBatch(day, department, snapshot, decisions, prices)
