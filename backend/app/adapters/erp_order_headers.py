"""按明确订单号读取稳定店铺归属；不按商品提示列推测缺失订单的影响范围。"""
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import httpx
from app.adapters.base import SourceUnavailable
from app.adapters.erp_bulk_capture import BLOCKED_LIST, _session
from app.adapters.erp_web import error_evidence
from app.adapters.normalization import identity_text
from app.services.erp_capture_store import write_verified

FIELDS = ('orderid','shopid','shopname','platform','status','orderNum','sellNum')


def candidate_order_request(capture):
    requested, issues = set(), []
    for group in capture['groups']:
        for number, value in enumerate(group['candidate_order_ids'], 1):
            try:
                requested.add(identity_text(value))
            except ValueError:
                issues.append({'code':'invalid_candidate_order_id', 'source':group.get('source'), 'row_number':number})
    return sorted(requested), issues


def capture_order_headers(settings, capture, *, client=None, web_session=None, batch_size=1000, progress=None):
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError('订单归属每批须为1至1000')
    if client is not None and web_session is not None:
        raise ValueError('共享会话不能与独立客户端混用')
    requested, issues = candidate_order_request(capture)
    source_id = 'erp-order-headers-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'erp-order-headers' / source_id
    directory.mkdir(mode=0o700, parents=True)
    result = {'schema_version':'erp-order-headers-v1','source_id':source_id,
        'erp_capture_id':capture['capture_id'],'synthetic':bool(capture.get('synthetic',False)),
        'sample_only':bool(capture.get('sample_only',False)),
        'queried_at':datetime.now(timezone.utc).isoformat(),'requested_order_ids':requested,
        'orders':{},'files':[],'issues':issues,'artifact_file':str((directory/'result.json').resolve())}
    try:
        with (nullcontext(web_session) if web_session is not None else _session(settings,client)) as session:
            post, _ = session
            for offset in range(0,len(requested),batch_size):
                batch = requested[offset:offset+batch_size]
                if progress:
                    progress(f'核对订单稳定店铺归属：{offset+1}至{offset+len(batch)}。')
                body = {'page':1,'limit':batch_size,'ptid':settings.erp_capture_platform_id,
                    'blocs':settings.erp_capture_bloc,'filter1':'r.orderid','filter1value':'\n'.join(batch)}
                try:
                    data = post(BLOCKED_LIST,body)
                    rows = data.get('records') if isinstance(data,dict) else None
                    total = data.get('total') if isinstance(data,dict) else None
                    projected = [{key:row.get(key) for key in FIELDS} for row in rows
                        if isinstance(row,dict)] if isinstance(rows,list) else []
                    path = directory / f'batch-{offset//batch_size:04d}.json'
                    # 订单列表可能包含客户资料；此来源只存业务身份白名单。
                    write_verified(path,{'request':body,'total':total,'records':projected})
                    result['files'].append({'file':str(path.resolve())})
                    if not isinstance(rows, list):
                        result['issues'].append({'code': 'order_header_response_invalid', 'order_ids': batch})
                        continue
                    requested_orders = set(batch)
                    by_order = defaultdict(list)
                    for row in projected:
                        order = row['orderid']
                        if isinstance(order, str) and order in requested_orders:
                            by_order[order].append(row)
                    verified = {}
                    for order, matches in by_order.items():
                        if len(matches) != 1:
                            continue
                        row = matches[0]
                        try:
                            identity_text(row['shopid']); identity_text(row['shopname'])
                        except ValueError:
                            continue
                        if str(row['platform']).casefold() == 'tiktok':
                            verified[order] = {**row, 'ref': f'{source_id}/{order}'}
                    result['orders'].update(verified)
                    if missing := requested_orders - verified.keys():
                        result['issues'].append({'code': 'order_header_unverified', 'order_ids': sorted(missing)})
                except (SourceUnavailable,httpx.HTTPError,ValueError,TypeError,KeyError) as error:
                    result['issues'].append({'code':'order_header_batch_unverified','order_ids':batch,
                        **error_evidence(error)})
    except (SourceUnavailable,httpx.HTTPError) as error:
        result['issues'].append({'code':'order_header_source_unavailable','order_ids':requested, **error_evidence(error)})
    result['rate_limit_observed'] = any(i.get('error_kind') == 'rate_limited' for i in result['issues'])
    result['acquisition_complete'] = set(result['orders'])==set(requested) and not result['issues']
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_verified(result['artifact_file'],result)
    return result
