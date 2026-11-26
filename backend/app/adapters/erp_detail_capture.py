"""明确订单号的原生商品明细批量采集，不分摊毛利或制造原生明细ID。

合同：ERP前端69763的asyncOrderDetailDownload/orderIdList和orderDetailList任务。
XLSX物理行全部保留；跨来源订单只请求一次，同SKU合法多行不去重。
"""
from datetime import datetime, timezone
from contextlib import nullcontext
from pathlib import Path
import re
from uuid import uuid4

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.erp_bulk_capture import EXPORT_POLL_LIMIT, TASKS, _save_xlsx, _session, _value
from app.adapters.erp_export_mapping import ERP_COLUMNS, _xlsx_rows
from app.adapters.erp_order_headers import candidate_order_request
from app.adapters.erp_web import error_evidence
from app.services.erp_capture_store import write_verified


DETAIL_EXPORT = '/newIp/report/orderDownload/asyncOrderDetailDownload'
TASK_SOURCE = 'orderDetailList'


class _DetailIssue(SourceUnavailable):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def capture_erp_details(settings, capture, *, client=None, web_session=None, batch_size=1000, max_polls=EXPORT_POLL_LIMIT, progress=None):
    if web_session is not None and client is not None:
        raise ValueError('共享ERP会话与独立客户端不能混用')
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError('ERP明细受控批大小须为1至1000')
    if type(max_polls) is not int or not 1 <= max_polls <= EXPORT_POLL_LIMIT:
        raise ValueError(f'ERP明细轮询次数须为1至{EXPORT_POLL_LIMIT}')
    requested, issues = candidate_order_request(capture)
    source_id = 'erp-details-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'erp-detail-captures' / source_id
    directory.mkdir(mode=0o700, parents=True)
    result = {
        'schema_version':'erp-order-details-v1', 'source_id':source_id,
        'erp_capture_id':capture['capture_id'], 'synthetic':bool(capture.get('synthetic', False)),
        'sample_only':bool(capture.get('sample_only', False)),
        'queried_at':datetime.now(timezone.utc).isoformat(), 'requested_order_ids':requested,
        'batches':[{'number':offset//batch_size+1, 'order_ids':requested[offset:offset+batch_size],
                    'status':'not_attempted', 'polls':[]} for offset in range(0, len(requested), batch_size)],
        'rows':[], 'files':[], 'issues':issues, 'pricing_ready':False,
        'artifact_file':str((directory / 'result.json').resolve()),
    }

    def issue(code, **scope):
        result['issues'].append({'code':code, **scope})

    def checkpoint():
        result['rate_limit_observed'] = any(i.get('error_kind') == 'rate_limited' for i in result['issues'])
        result['acquisition_complete'] = not result['issues'] and all(
            batch['status'] == 'complete' for batch in result['batches'])
        result['summary'] = {'requested_orders':len(requested),
            'observed_orders':len({row['order_id'] for row in result['rows']}),
            'physical_rows':len(result['rows'])}
        write_verified(result['artifact_file'], result)

    checkpoint()
    try:
        if requested:
            with (nullcontext(web_session) if web_session is not None else _session(settings, client)) as (post, download):
                for batch in result['batches']:
                    number = batch['number']
                    if progress:
                        progress(f'读取ERP原生明细：第{number}/{len(result["batches"])}批，{len(batch["order_ids"])}单。')
                    try:
                        batch['status'] = 'submitting'
                        response = post(DETAIL_EXPORT, {'orderIdList':batch['order_ids']})
                        match = re.search(r'下载任务【([^】]+)】', response) if isinstance(response, str) else None
                        if not match:
                            raise _DetailIssue('detail_task_not_bound')
                        batch['task_name'] = match[1]
                        batch['status'] = 'polling'
                        for attempt in range(max_polls):
                            data = post(TASKS, {'pageNum':1, 'pageSize':100, 'source':TASK_SOURCE})
                            records = data.get('records') if isinstance(data, dict) else data
                            if not isinstance(records, list):
                                raise _DetailIssue('detail_task_response_invalid')
                            matches = [row for row in records if isinstance(row, dict)
                                and row.get('source') == TASK_SOURCE and row.get('fileName') == batch['task_name']]
                            if len(matches) > 1:
                                raise _DetailIssue('detail_task_ambiguous')
                            task = matches[0] if matches else None
                            batch['polls'].append({'attempt':attempt+1,
                                'queried_at':datetime.now(timezone.utc).isoformat(),
                                'status':task.get('status') if task else 'not_found'})
                            if task is None:
                                continue
                            if task.get('id') is None or ('task_id' in batch and batch['task_id'] != task['id']):
                                raise _DetailIssue('detail_task_id_changed')
                            batch['task_id'] = task['id']
                            if task.get('status') in ('failure', 'failed', 'interrupted'):
                                raise _DetailIssue('detail_task_failed')
                            if task.get('status') != 'success':
                                continue
                            raw = download(task.get('downloadUrl'))
                            batch['downloaded_at'] = datetime.now(timezone.utc).isoformat()
                            original = _save_xlsx(directory, f'order-details-{number:03d}.xlsx', raw)
                            result['files'].append(original)
                            expected = set(batch['order_ids'])
                            rows = []
                            excluded_count = 0
                            for row_number, fields in _xlsx_rows(raw, ERP_COLUMNS, 'ERP原生商品明细'):
                                order = fields['订单编号']
                                if not isinstance(order, str) or order not in expected:
                                    issue('detail_scope_mismatch', batch=number, row_number=row_number)
                                    excluded_count += 1
                                    continue
                                rows.append({'order_id':order, 'row_number':row_number,
                                    'fields':{key:_value(value) for key,value in fields.items()},
                                    'ref':f'erp-detail-{original["sha256"]}-r{row_number}'})
                            observed = {row['order_id'] for row in rows}
                            missing = sorted(expected - observed)
                            batch['missing_order_ids'] = missing
                            result['rows'].extend(rows)
                            batch['status'] = 'partial' if missing or excluded_count else 'complete'
                            if missing:
                                issue('detail_orders_missing', batch=number, order_ids=missing)
                            break
                        else:
                            raise _DetailIssue('detail_task_still_pending')
                    except (SourceUnavailable, httpx.HTTPError, ValueError) as error:
                        pending = isinstance(error,_DetailIssue) and error.code=='detail_task_still_pending'
                        batch['status'] = 'pending' if pending else 'failed'
                        issue(error.code if isinstance(error, _DetailIssue) else 'detail_request_failed',
                              batch=number, **error_evidence(error))
                    finally:
                        checkpoint()
    except (SourceUnavailable, httpx.HTTPError, ValueError) as error:
        issue('detail_source_failed', **error_evidence(error))
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return result
