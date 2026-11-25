"""固定名单逐条上传单商品含税价，提交后仅查询本次调价任务。"""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx

from app.adapters.base import LookupResult, SourceUnavailable
from app.adapters.erp_order_loss import _warehouse_matches
from app.adapters.manual_live import ManualLiveAdapter, effective_marketing_fields, validate_authorization
from app.adapters.normalization import money
from app.adapters.sales_auth import SalesAuth
from app.adapters.sales_platform_capture import capture_platform_controls
from app.domain.money import MoneyRatio
from app.domain.engine import SITE_CURRENCIES
from app.domain.types import OnlineState
from app.services.store import BEIJING, utcnow


# 五站点共用单品合同；允许执行不等于已真实核验生效。
VERIFIED_FULFILLMENT = {site: frozenset({'direct', 'overseas'}) for site in SITE_CURRENCIES}
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
            tolerance = Decimal('1') if key.site == 'VN' else Decimal('0.01')
            expected_parent = (shop_name, key.shop_id, key.product_id, online.main_sku)
            if (
                tuple(parent.get(field) for field in ('shopName', 'shopId', 'productId', 'localMainSku')) != expected_parent
                or data['id'] != key.product_id
                or tuple(variant.get(field) for field in ('shopName', 'shopId', 'productId', 'sellerSku')) !=
                    (shop_name, key.shop_id, key.product_id, key.sku)
                or sku['seller_sku'] != key.sku
                or variant['currency'] != online.currency or sku['price']['currency'] != online.currency
                or (online.profit_check.overseas_stock == 0 and stock != 0)
                or current_fulfillment != online.profit_check.mode
                or not _warehouse_matches(variant, key.warehouse)
                or retail <= 0 or actual <= 0 or money(sku['price']['sale_price']) <= 0
                or abs(retail - online.retail_price) > tolerance
                or abs(money(sku['price']['sale_price']) - retail) > tolerance
                or abs(actual - baseline_actual) > tolerance
                or not 0 <= observed_discount < 100 or observed_factor != baseline_factor
                or observed.astimezone(BEIJING).date() != business_date
                or not timedelta(0) <= utcnow() - observed <= timedelta(minutes=15)
            ):
                raise ValueError('提交前商品、仓配或现价与算价依据不一致')
            authorization['discount_percent_off'] = str(observed_discount)
            adapter = self._single(execution_id, authorization)
            if activity is True:
                adapter._promotion(variant)
            elif activity is not False or observed_discount != 0:
                raise ValueError('提交前折扣活动未得到确认')
            # 上传前记录已有日志，以便结果未知时只认本次新增任务。
            previous_ids = [str(row['id']) for row in adapter._logs()]
        except (httpx.HTTPError, KeyError, TypeError, ArithmeticError) as exc:
            raise SourceUnavailable('提交前无法确认单品当前价格或操作日志') from exc
        return {'authorization': authorization, 'previous_log_ids': previous_ids,
                'platform_source': platform['source_id'], 'observed_at': observed.isoformat()}

    def submit(self, execution_id, decision):
        proof = decision.trace[ROSTER_UPLOAD_TRACE]
        adapter = self._single(execution_id, proof['authorization'])
        result = adapter.upload_confirmed_price(execution_id, decision, proof['previous_log_ids'])
        if result.status == 'accepted':
            result.message = '上传已受理，等待调价任务结果'
        elif result.status == 'unknown':
            result.message = '上传结果未知，只查询调价任务，不重传'
        return result

    def lookup(self, execution_id, task_id, decision) -> LookupResult:
        try:
            authorization = decision.trace[ROSTER_UPLOAD_TRACE]['authorization']
            if money(authorization['upload_price']) != decision.upload_price:
                return LookupResult('unknown', message='授权金额与本次执行不一致')
            adapter = self._single(execution_id, authorization)
            _, _, matches = adapter._task_evidence(execution_id, decision.key, task_id)
            if len(matches) != 1:
                return LookupResult('unknown', message='本次调价任务尚未唯一匹配，只查询不重传')
            return adapter._task_outcome(matches[0])
        except Exception as exc:
            return LookupResult('unknown', message=f'调价任务查询未完成（{type(exc).__name__}），只查询不重传')
