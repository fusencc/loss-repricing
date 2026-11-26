"""仅查询历史固定名单调价任务；不再创建或提交名单调价。"""

from pathlib import Path
from uuid import UUID

import httpx

from app.adapters.base import LookupResult
from app.adapters.manual_live import ManualLiveAdapter
from app.adapters.normalization import money
from app.adapters.sales_auth import SalesAuth


ROSTER_UPLOAD_TRACE = 'erp_roster_upload'


class RosterPriceTransport:
    """保留历史执行的任务证据查询，未知结果只查不重传。"""

    def __init__(self, config, client=None):
        self.config = config
        self.client = client or httpx.Client(timeout=60, trust_env=False, follow_redirects=False)
        self.auth = SalesAuth(config, self.client)

    def close(self):
        self.client.close()

    def _single(self, execution_id, authorization):
        directory = Path(self.config.runtime_dir) / 'erp-roster-price-executions' / str(UUID(execution_id))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return ManualLiveAdapter(self.config, authorization, directory, client=self.client, auth=self.auth)

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
