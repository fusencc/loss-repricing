"""按部门算价结果上传单商品含税价，仅查询本次调价任务结果。"""
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx

from app.adapters.base import LookupResult, SourceUnavailable
from app.adapters.manual_live import ManualLiveAdapter, validate_authorization
from app.adapters.normalization import money
from app.adapters.sales_auth import SalesAuth
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
        # 本轮算价已确认商品与金额，上传前不重复查询现价、折扣和仓配。
        discount = (MoneyRatio(Decimal(1)) - baseline_factor) * MoneyRatio(Decimal(100))
        authorization['discount_percent_off'] = str(discount.decimal())
        adapter = self._single(execution_id, authorization)
        try:
            previous = [str(row['id']) for row in adapter._logs()]
        except httpx.HTTPError as error:
            raise SourceUnavailable('提交前调价任务日志读取失败') from error
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
            return adapter._task_outcome(matches[0])
        except (OSError, httpx.HTTPError, SourceUnavailable, KeyError, TypeError, ValueError, ArithmeticError) as error:
            return LookupResult('unknown', message=f'调价任务结果待查询（{type(error).__name__}）')
