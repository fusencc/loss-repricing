"""按部门算价结果上传单商品含税价，按本次任务回读在线成交价。"""
from datetime import datetime
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
from app.domain.types import OnlineState
from app.services.store import BEIJING, utcnow


ERP_UPLOAD_TRACE = 'erp_department_upload'


class ErpPriceTransport:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or httpx.Client(timeout=60, trust_env=False, follow_redirects=False)
        self.auth = SalesAuth(config, self.client)

    def close(self):
        self.client.close()

    def _single(self, execution_id, authorization):
        directory = Path(self.config.runtime_dir) / 'erp-department-price-executions' / str(UUID(execution_id))
        return ManualLiveAdapter(self.config, authorization, directory, client=self.client, auth=self.auth)

    def _current(self, execution_id, stage, authorization, key):
        shop = authorization['shop_name']
        target = {'erp_shop': shop, 'product_id': key.product_id, 'item_id': key.item_id}
        source = {'capture_id': f'erp-{stage}-{execution_id}', 'synthetic': False, 'sample_only': True}
        try:
            platform = capture_platform_controls(self.config, source, [target], client=self.client, auth=self.auth)
        except httpx.HTTPError as error:
            raise SourceUnavailable('在线商品价格读取失败') from error
        if not platform['records']:
            raise SourceUnavailable('未取得目标商品的在线价格')
        control, = platform['records']
        parent, product = control['company_parent'], control['data']
        variant, = (row for row in control['current_variants'] if row['sellerSkuId'] == key.item_id)
        sku, = (row for row in product['skus'] if row['id'] == key.item_id)
        if ((parent['shopName'], parent['shopId'], parent['productId'], parent['localMainSku'])
                != (shop, key.shop_id, key.product_id, authorization['main_sku'])
                or (variant['shopName'], variant['shopId'], variant['productId'], variant['sellerSku'])
                != (shop, key.shop_id, key.product_id, key.sku)
                or product['id'] != key.product_id or sku['seller_sku'] != key.sku
                or sku['price']['currency'] != authorization['currency'] or variant['currency'] != authorization['currency']
                or not _warehouse_matches(variant, key.warehouse)):
            raise ValueError('在线子商品的身份、仓库或币种与算价目标不符')
        retail = money(variant['priceIncludeVat'])
        tolerance = Decimal('1') if key.site == 'VN' else Decimal('.01')
        if retail <= 0 or abs(money(sku['price']['sale_price']) - retail) > tolerance:
            raise ValueError('平台与销售系统当前含税价不一致')
        actual, discount = effective_marketing_fields(variant, retail, control['company_activity_locked'])
        if actual <= 0 or not 0 <= discount < 100:
            raise ValueError('在线成交价或折扣无效')
        return control, variant, retail, actual, discount

    def prepare(self, execution_id, batch_id, business_date, shop_name, decision):
        online_row, = decision.trace['online_rows']
        online = OnlineState.model_validate(online_row)
        if online.key != decision.key:
            raise ValueError('在线价格证据的商品与本次执行不符')
        baseline = online.actual_price_and_discount()
        if baseline is None or online.profit_check is None:
            raise ValueError('算价依据缺少有效成交价或仓配')
        baseline_actual, baseline_factor = baseline
        authorization = {
            'id': batch_id, 'approved': True, 'actor': 'erp_department_batch',
            'business_date': business_date.isoformat(), 'key': decision.key.model_dump(mode='json'),
            'shop_name': shop_name, 'main_sku': online.main_sku, 'currency': online.currency,
            'baseline_retail': str(online.retail_price), 'baseline_actual': str(baseline_actual),
            'upload_price': str(decision.upload_price),
        }
        validate_authorization(authorization, decision.key, decision.upload_price)
        control, variant, retail, actual, discount = self._current(execution_id, 'submit', authorization, decision.key)
        tolerance = Decimal('1') if decision.key.site == 'VN' else Decimal('.01')
        raw_stock = variant.get('overseasStockSum')
        stock = None if raw_stock in (None, '') else money(raw_stock)
        mode = 'overseas' if stock is not None and stock > 0 else 'direct'
        factor = MoneyRatio(Decimal(1)) - MoneyRatio(discount, Decimal(100))
        if (abs(online.retail_price - retail) > tolerance or abs(baseline_actual - actual) > tolerance
                or factor != baseline_factor or mode != online.profit_check.mode):
            raise ValueError('提交前现价、折扣或仓配与算价依据不一致')
        authorization['discount_percent_off'] = str(discount)
        adapter = self._single(execution_id, authorization)
        try:
            if control['company_activity_locked']:
                adapter._promotion(variant)
            previous = [str(row['id']) for row in adapter._logs()]
        except httpx.HTTPError as error:
            raise SourceUnavailable('提交前活动或调价任务日志读取失败') from error
        return {'authorization': authorization, 'previous_log_ids': previous}

    def submit(self, execution_id, decision):
        proof = decision.trace[ERP_UPLOAD_TRACE]
        adapter = self._single(execution_id, proof['authorization'])
        return adapter.upload_confirmed_price(execution_id, decision, proof['previous_log_ids'])

    def lookup(self, execution_id, task_id, decision):
        try:
            authorization = decision.trace[ERP_UPLOAD_TRACE]['authorization']
            if money(authorization['upload_price']) != decision.upload_price:
                return LookupResult('unknown', message='执行金额与上传记录不一致')
            adapter = self._single(execution_id, authorization)
            today = utcnow().astimezone(BEIJING).date().isoformat()
            _, _, matches = adapter._task_evidence(execution_id, decision.key, task_id, through=today)
            if len(matches) != 1:
                return LookupResult('unknown', message='本次调价任务尚未唯一匹配')
            outcome = adapter._task_outcome(matches[0])
            if outcome.status != 'accepted':
                return outcome
            control, _, _, actual, _ = self._current(execution_id, 'verify', authorization, decision.key)
            # 营销价来自销售系统子商品行，使用该请求的实测接收时间。
            observed = datetime.fromisoformat(control['variants_observed_at'])
            return LookupResult('accepted', actual, observed, message=outcome.message)
        except (OSError, httpx.HTTPError, SourceUnavailable, KeyError, TypeError, ValueError, ArithmeticError) as error:
            return LookupResult('unknown', message=f'调价任务或在线成交价待回查（{type(error).__name__}）')
