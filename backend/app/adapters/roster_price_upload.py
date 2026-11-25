"""固定名单逐条复用已实测的单商品含税上传和平台实价核验。"""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.erp_order_loss import _warehouse_matches
from app.adapters.manual_live import ManualLiveAdapter, effective_marketing_fields, validate_authorization
from app.adapters.normalization import money
from app.adapters.sales_auth import SalesAuth
from app.adapters.sales_platform_capture import capture_platform_controls
from app.domain.money import MoneyRatio
from app.domain.types import OnlineState
from app.services.store import BEIJING, utcnow


# 既有真实回读范围及 2026-11-25 用户明确授权续提的 PH 直发、TH 海外仓、VN 范围。
# 新授权组合逐条读取真实现价、上传及回读；授权开放不代表已经核验生效。
VERIFIED_FULFILLMENT = {
    'MY': frozenset({'direct', 'overseas'}),
    'PH': frozenset({'direct', 'overseas'}),
    'TH': frozenset({'direct', 'overseas'}),
    'VN': frozenset({'direct', 'overseas'}),
}
ROSTER_UPLOAD_TRACE = 'erp_roster_upload'


class RosterPriceTransport:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or httpx.Client(timeout=60, trust_env=False, follow_redirects=False)
        self.auth = SalesAuth(config, self.client)

    @property
    def available(self):
        return self.auth.configured

    def close(self):
        self.client.close()

    def _single(self, execution_id, authorization):
        directory = Path(self.config.runtime_dir) / 'erp-roster-price-executions' / str(UUID(execution_id))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return ManualLiveAdapter(self.config, authorization, directory, client=self.client, auth=self.auth)

    def prepare(self, execution_id, batch_id, business_date, shop_name, decision):
        key = decision.key
        rows = decision.trace.get('online_rows', [])
        if len(rows) != 1:
            raise ValueError('提交需要唯一在线子商品')
        online = OnlineState.model_validate(rows[0])
        baseline = online.actual_price_and_discount()
        if (online.key != key or not online.profit_check or baseline is None
                or online.profit_check.mode not in VERIFIED_FULFILLMENT.get(key.site, ())):
            raise ValueError('子商品站点、仓配或当前折扣不在已核验的单品执行范围')
        baseline_actual, baseline_factor = baseline
        authorization = {
            'id': batch_id, 'approved': True, 'actor': 'erp_roster_batch',
            'business_date': business_date.isoformat(), 'key': key.model_dump(mode='json'),
            'shop_name': shop_name, 'main_sku': online.main_sku, 'currency': online.currency,
            'baseline_retail': str(online.retail_price), 'baseline_actual': str(baseline_actual),
            'upload_price': str(decision.upload_price),
        }
        validate_authorization(authorization, key, decision.upload_price)
        try:
            # ERP 导出已按原销售员重算。这里只核对会影响实际上传对象和成交价的当前状态。
            source = {'capture_id': 'roster-submit-' + execution_id, 'synthetic': False, 'sample_only': True}
            targets = [{'erp_shop': shop_name, 'product_id': key.product_id, 'item_id': key.item_id}]
            platform = capture_platform_controls(self.config, source, targets, client=self.client, auth=self.auth)
            if not platform['acquisition_complete'] or len(platform['records']) != 1:
                raise SourceUnavailable('提交前未取得完整的销售与平台价格')
            control = platform['records'][0]
            parent = control['company_parent']
            data = control['data']
            variants = [row for row in control['current_variants'] if row['sellerSkuId'] == key.item_id]
            skus = [row for row in data['skus'] if row['id'] == key.item_id]
            if len(variants) != 1 or len(skus) != 1:
                raise ValueError('当前在线子商品身份不唯一')
            variant, sku = variants[0], skus[0]
            retail = money(variant['priceIncludeVat'])
            activity = control['company_activity_locked']
            actual, observed_discount = effective_marketing_fields(variant, retail, activity)
            # 算价快照可能只保存两种当前价格。实时 OFF 必须与本轮用的精确成交系数吻合。
            observed_factor = MoneyRatio(Decimal(1)) - MoneyRatio(observed_discount, Decimal(100))
            observed = datetime.fromisoformat(control['observed_at'])
            raw_stock = variant.get('overseasStockSum')
            stock = None if raw_stock in (None, '') else money(raw_stock)
            current_fulfillment = 'overseas' if stock is not None and stock > 0 else 'direct'
            expected_parent = (shop_name, key.shop_id, key.product_id, online.main_sku)
            if (
                tuple(parent.get(field) for field in ('shopName', 'shopId', 'productId', 'localMainSku')) != expected_parent
                or data['id'] != key.product_id
                or tuple(variant.get(field) for field in ('shopName', 'shopId', 'productId', 'sellerSku')) !=
                    (shop_name, key.shop_id, key.product_id, key.sku)
                or sku['seller_sku'] != key.sku
                or parent.get('productStatus') != 4
                or data.get('product_status') != 'ACTIVATE' or data.get('status') != 'ACTIVATE'
                or data.get('is_not_for_sale') is not False
                or sku.get('status_info', {}).get('status') != 'NORMAL'
                or variant['currency'] != online.currency or sku['price']['currency'] != online.currency
                or (online.profit_check.overseas_stock == 0 and stock != 0)
                or current_fulfillment != online.profit_check.mode
                or not _warehouse_matches(variant, key.warehouse)
                or retail != online.retail_price or money(sku['price']['sale_price']) != retail
                or actual != baseline_actual or not 0 <= observed_discount < 100 or observed_factor != baseline_factor
                or observed.astimezone(BEIJING).date() != business_date
                or not timedelta(0) <= utcnow() - observed <= timedelta(minutes=5)
            ):
                raise ValueError('提交前商品、仓配或现价与算价依据不一致')
            authorization['discount_percent_off'] = str(observed_discount)
            adapter = self._single(execution_id, authorization)
            if activity is True:
                adapter._promotion(variant)
            elif activity is not False or observed_discount != 0:
                raise ValueError('提交前折扣活动未得到确认')
            # 只查本店本产品当日日志。回读识别新任务必须有上传前基线，查询失败不发送。
            previous_ids = [str(row['id']) for row in adapter._logs()]
        except (httpx.HTTPError, KeyError, TypeError, ArithmeticError) as exc:
            raise SourceUnavailable('提交前无法确认单品当前价格或操作日志') from exc
        return {'authorization': authorization, 'previous_log_ids': previous_ids,
                'platform_source': platform['source_id'], 'observed_at': observed.isoformat()}

    def submit(self, execution_id, decision):
        proof = decision.trace[ROSTER_UPLOAD_TRACE]
        adapter = self._single(execution_id, proof['authorization'])
        return adapter.upload_confirmed_price(execution_id, decision, proof['previous_log_ids'])

    def lookup(self, execution_id, task_id, decision):
        proof = decision.trace[ROSTER_UPLOAD_TRACE]
        authorization = proof['authorization']
        if money(authorization['upload_price']) != decision.upload_price:
            raise ValueError('单品回读授权金额与持久执行不一致')
        adapter = self._single(execution_id, authorization)
        # 上传绑定/平台实价由单品接口提供，价格偏差、参考上限、利润和站点暂停由统一服务判断。
        return adapter.lookup(execution_id, task_id, decision.key, check_amount=False)
