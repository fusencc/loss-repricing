"""前七个完整北京时间自然日的主SKU件数：订单名单→完整明细→主SKU目录。

沿用有效订单来源范围；付款日期使用ERP前端的yyyy-MM-dd查询。
orderNum为订单总件数、sellNum为SKU种数，已用116单/122行/147件逐单核对。
不消费GoodsSales接口没有窗口的销量字段。店铺配置响应只存业务身份投影。
"""
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import json

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.erp_bulk_capture import BLOCKED_LIST, _session, _value
from app.adapters.erp_detail_capture import capture_erp_details
from app.adapters.erp_export_mapping import ERP_COLUMNS, _xlsx_rows
from app.adapters.erp_sku_catalog import capture_sku_catalog
from app.adapters.erp_web import ErpAccountRateLimited
from app.adapters.normalization import identity_text, money, sku_text, source_time
from app.domain.engine import BEIJING, sales_window
from app.services.erp_capture_store import write_verified

VALID_STATUSES = {'2','3','4','5','6','7'}
VALID_LABELS = {'已支付','待处理','可配货','配货中','已发货','已完成'}
WINDOW_COLUMNS = (*ERP_COLUMNS,'订单付款时间','订单状态')


def _integer(value):
    number = money(_value(value))
    if number <= 0 or number != number.to_integral_value():
        raise ValueError('sales_window_quantity_invalid')
    return int(number)


def _ref(source):
    raw = Path(source['artifact_file']).read_bytes()
    restored = json.loads(raw)
    if restored != source:
        raise OSError('销量来源回读不一致')
    return {'source_id':source['source_id'],'artifact_file':source['artifact_file'],
            'sha256':sha256(raw).hexdigest(),'acquisition_complete':source['acquisition_complete']}


def capture_sales_window(settings, capture, targets, *, client=None, web_session=None,
                         catalog_client=None, page_size=10000, progress=None):
    if type(page_size) is not int or not 1 <= page_size <= 10000:
        raise ValueError('销量订单页大小须为1至10000')
    if web_session is not None and client is not None:
        raise ValueError('销量共享会话与独立客户端不能混用')
    start, end = sales_window(source_time(capture['queried_at']))
    plans = defaultdict(list)
    for target in targets:
        for field in ('erp_shop','shop_id','erp_sku','main_sku'):
            identity_text(target[field])
        plans[target['erp_shop']].append(target)
    source_id = 'erp-sales-window-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'erp-sales-windows' / source_id
    directory.mkdir(mode=0o700, parents=True)
    result = {'schema_version':'erp-complete-sales-window-v1','source_id':source_id,
        'erp_capture_id':capture['capture_id'],'synthetic':bool(capture.get('synthetic',False)),
        'sample_only':bool(capture.get('sample_only',False)),
        'queried_at':datetime.now(timezone.utc).isoformat(),'window_start':str(start),'window_end':str(end),
        'query_timezone':'Asia/Shanghai','requested_shops':sorted(plans),'files':[],
        'order_lists':{},'rows':[],'volumes':[],'issues':[],'pricing_ready':False,
        'native_source':None,'catalog_source':None,'artifact_file':str((directory/'result.json').resolve())}
    bad, orders = set(), {}

    def issue(code, shops):
        bad.update(shops)
        result['issues'].append({'code':code,'shops':sorted(shops)})

    def save(name, data):
        path = directory / name
        write_verified(path,data)
        raw = path.read_bytes()
        result['files'].append({'file':str(path.resolve()),'bytes':len(raw),'sha256':sha256(raw).hexdigest()})

    try:
        if plans:
            with (nullcontext(web_session) if web_session is not None else _session(settings,client)) as session:
                post, _ = session
                catalog = post('/Api/order/findshop2/'+settings.erp_capture_platform_id,None,success_code=0)
                if not isinstance(catalog,list):
                    raise ValueError('sales_window_shop_catalog_invalid')
                # 该配置DTO可能带认证字段，禁止原件落盘；只保存所需店铺身份。
                shop_rows = [{key:row.get(key) for key in ('name','shopid','platid','platname','bloc')}
                    for row in catalog if isinstance(row,dict) and row.get('name') in plans]
                save('shop-identities.json',{'projection':'business_identity_only','records':shop_rows})
                for shop in sorted(plans):
                    matches = [row for row in shop_rows if row['name']==shop
                        and str(row['platname']).casefold()=='tiktok']
                    supplied = {target['shop_id'] for target in plans[shop]}
                    if len(matches)!=1 or supplied!={matches[0]['shopid']}:
                        issue('sales_window_shop_identity_unverified',[shop])
                        continue
                    page, total, seen = 1, None, set()
                    try:
                        while True:
                            if progress:
                                progress(f'读取完整七日销量订单：{shop}，第{page}页。')
                            body = {'page':page,'limit':page_size,'shopid':matches[0]['shopid'],
                                'ptid':settings.erp_capture_platform_id,'blocs':settings.erp_capture_bloc,
                                'manyStatus':'2,3,4,5,6,7','time':'r.order_time','starttime':str(start),'endtime':str(end)}
                            data = post(BLOCKED_LIST,body)
                            rows, count = data.get('records'), data.get('total')
                            projected = [{key:row.get(key) for key in (
                                'orderid','shopid','shopname','platform','status','orderTime','orderNum','sellNum')}
                                for row in rows if isinstance(row,dict)] if isinstance(rows,list) else []
                            save(f'orders-{len(result["files"]):05d}.json',{'request':body,'total':count,'records':projected})
                            if not isinstance(rows,list) or len(projected)!=len(rows) or type(count) is not int or count<0:
                                raise ValueError('sales_window_order_list_invalid')
                            if total is None:
                                total = count
                            if count!=total or len(rows)!=min(page_size,max(0,total-len(seen))):
                                raise ValueError('sales_window_pagination_incomplete')
                            for row in projected:
                                order = identity_text(row['orderid'])
                                paid = datetime.strptime(row['orderTime'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING)
                                if (order in seen or order in orders or row['shopname']!=shop
                                        or row['shopid']!=matches[0]['shopid'] or str(row['platform']).casefold()!='tiktok'
                                        or str(row['status']) not in VALID_STATUSES or not start<=paid.date()<=end):
                                    raise ValueError('sales_window_order_scope_mismatch')
                                row['orderNum'], row['sellNum'] = _integer(row['orderNum']), _integer(row['sellNum'])
                                seen.add(order)
                                orders[order] = row
                            if len(seen)==total:
                                result['order_lists'][shop] = sorted(seen)
                                break
                            page += 1
                    except (SourceUnavailable,httpx.HTTPError,ValueError,TypeError,ArithmeticError) as error:
                        issue('sales_window_order_list_failed',[shop])
                        if isinstance(error,ErpAccountRateLimited) or (isinstance(error,httpx.HTTPStatusError)
                                and error.response.status_code in (401,403)):
                            issue('sales_window_source_stopped',set(plans)-set(result['order_lists']))
                            break
                selected = [order for order,row in orders.items() if row['shopname'] not in bad]
                detail_scope = {'capture_id':capture['capture_id'],'synthetic':result['synthetic'],
                    'sample_only':result['sample_only'],'groups':[{'candidate_order_ids':selected}]}
                native = capture_erp_details(settings,detail_scope,web_session=session,progress=progress)
                result['native_source'] = _ref(native)
                accepted = {row['ref'] for row in native['rows']}
                for entry in native['files']:
                    raw = Path(entry['file']).read_bytes()
                    if len(raw)!=entry['bytes'] or sha256(raw).hexdigest()!=entry['sha256']:
                        raise OSError('销量明细原件哈希不一致')
                    try:
                        for number,fields in _xlsx_rows(raw,WINDOW_COLUMNS,'七日销量原始明细'):
                            ref = f'erp-detail-{entry["sha256"]}-r{number}'
                            if ref not in accepted:
                                continue
                            order = fields['订单编号']
                            owner = orders[order]['shopname']
                            paid = fields['订单付款时间']
                            if (str(fields['平台']).casefold()!='tiktok' or fields['店铺']!=owner
                                    or not isinstance(paid,datetime) or not start<=paid.date()<=end
                                    or fields['订单状态'] not in VALID_LABELS):
                                issue('sales_window_native_scope_mismatch',[owner])
                                continue
                            result['rows'].append({'order_id':order,'erp_shop':owner,
                                'erp_sku':sku_text(fields['商品SKU列表']),'quantity':_integer(fields['订单数量']),
                                'paid_at':paid.replace(tzinfo=BEIJING).isoformat(),'native_ref':ref})
                    except (SourceUnavailable,ValueError,TypeError,ArithmeticError,KeyError):
                        issue('sales_window_native_fields_invalid',plans)
                by_order = defaultdict(list)
                for row in result['rows']:
                    by_order[row['order_id']].append(row)
                for order in selected:
                    rows = by_order[order]
                    header = orders[order]
                    if (sum(row['quantity'] for row in rows)!=header['orderNum']
                            or len({row['erp_sku'] for row in rows})!=header['sellNum']):
                        issue('sales_window_native_order_incomplete',[header['shopname']])
    except (SourceUnavailable,httpx.HTTPError,ValueError,TypeError,ArithmeticError):
        issue('sales_window_read_failed',plans)
    skus = {row['erp_sku'] for row in result['rows']} | {t['erp_sku'] for values in plans.values() for t in values}
    catalog = capture_sku_catalog(settings,skus,erp_capture_id=capture['capture_id'],
        synthetic=result['synthetic'],sample_only=result['sample_only'],client=catalog_client,progress=progress)
    result['catalog_source'] = _ref(catalog)
    mapping = catalog['mapping']
    totals = defaultdict(int)
    for row in result['rows']:
        if row['erp_sku'] not in mapping:
            issue('sales_window_main_sku_unknown',[row['erp_shop']])
        else:
            totals[row['erp_shop'],mapping[row['erp_sku']]] += row['quantity']
    for shop, values in plans.items():
        if shop not in result['order_lists']:
            bad.add(shop)
        for target in values:
            if mapping.get(target['erp_sku'])!=target['main_sku']:
                issue('sales_window_target_main_sku_mismatch',[shop])
        for main in sorted({target['main_sku'] for target in values}):
            complete = shop not in bad
            result['volumes'].append({'erp_shop':shop,'main_sku':main,
                'quantity':totals[shop,main] if complete else None,'complete':complete})
    result['acquisition_complete'] = not bad and not result['issues']
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_verified(result['artifact_file'],result)
    return result
