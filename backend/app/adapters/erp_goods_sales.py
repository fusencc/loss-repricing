"""ERP开放接口子SKU＋仓库七日销量，仅供参考；不扫描店铺、不作为保护条件。

依据2026-11-17开放接口实测及用户更新：skuList批量查询，storageSalesList.sevenDays
是件数。逐仓保留，不冒充店铺/主SKU销量，不以缺失填0，不编造统计起止日期。
"""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
import time
from uuid import uuid4

import httpx

from app.adapters.erp_rates import ALLOWED_BASES
from app.adapters.transport_privacy import private_request
from app.services.erp_capture_store import write_verified

BASIS = 'erp_goods_warehouse_seven_days_reference'
SCHEMA = 'erp-goods-sales-reference-v1'
PATH = '/goods/queryGoodsSales'


def capture_goods_sales(settings, capture, targets, *, client=None, progress=None,
                        batch_size=1000, interval_seconds=16):
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError('销量批大小须为1至1000')
    requested = sorted({t['erp_sku'] for t in targets})
    if any(not isinstance(s,str) or not s.strip() or s != s.strip() for s in requested):
        raise ValueError('候选ERP子SKU无效')
    now = lambda: datetime.now(timezone.utc).isoformat()
    sid = 'erp-goods-sales-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'erp-goods-sales' / sid
    directory.mkdir(parents=True, mode=0o700)
    result = dict(schema_version=SCHEMA, source_id=sid, erp_capture_id=capture['capture_id'],
        synthetic=capture.get('synthetic',False), sample_only=capture.get('sample_only',False),
        basis=BASIS, policy='reference_only', queried_at=now(), requested_skus=requested,
        window_start=None, window_end=None, records={}, queries=[], issues=[],
        artifact_file=str((directory/'result.json').resolve()))
    owned = client is None
    client = client or httpx.Client(timeout=20, trust_env=False, follow_redirects=False)
    context = private_request.set(True)
    last_request = None
    base = settings.erp_open_api_url.rstrip('/')
    def payload(response):
        response.raise_for_status()
        data = response.json()
        if not isinstance(data,dict) or data.get('success') is not True or data.get('code') != 200:
            raise ValueError('goods_sales_business_error')
        return data
    try:
        if base not in ALLOWED_BASES:
            raise ValueError('goods_sales_unconfirmed_origin')
        token = settings.erp_token
        if requested and not token:
            login = payload(client.post(base+'/system/innerOpen/login',json={
                'username':settings.erp_username,'password':settings.erp_password.get_secret_value()},timeout=20))
            data = login.get('data')
            token = data.get('accessToken') or data.get('token') if isinstance(data,dict) else None
        if requested and (not isinstance(token,str) or not token.strip()):
            raise ValueError('goods_sales_authentication_failed')
        token = re.sub(r'^Bearer\s+', '', token or '',flags=re.IGNORECASE).strip()
        for offset in range(0,len(requested),batch_size):
            batch=requested[offset:offset+batch_size]
            page,total,seen=1,None,set()
            while True:
                body={'skuList':batch,'pageNum':page,'pageSize':1000}
                try:
                    # 16秒为实测成功间隔，并非官方配额；限流只重试一次，避免长时间占用。
                    for attempt in range(2):
                        if last_request is not None:
                            time.sleep(max(0,interval_seconds-(time.monotonic()-last_request)))
                        last_request=time.monotonic()
                        if progress:progress(f'开放接口参考销量：第{offset//batch_size+1}批，第{page}页。')
                        response=client.post(base+PATH,json=body,headers={'Authorization':'Bearer '+token},timeout=20)
                        path=directory/f'{len(result["queries"])+1:04d}.json'
                        path.write_bytes(response.content)
                        result['queries'].append(dict(request=body,http_status=response.status_code,
                            response_file=str(path.resolve()),sha256=sha256(path.read_bytes()).hexdigest(),observed_at=now()))
                        value=response.json() if response.status_code==200 else {}
                        if attempt==0 and isinstance(value,dict) and '限流' in str(value.get('msg','')):
                            continue
                        value=payload(response)
                        break
                    rows=value.get('data');count=value.get('total');more=value.get('hasNext')
                    if (not isinstance(rows,list) or type(count) is not int or count<0
                            or type(more) is not bool or (total is not None and total!=count)):
                        raise ValueError('goods_sales_pagination_invalid')
                    total=count
                    if total>len(batch) or (not rows and more):raise ValueError('goods_sales_scope_invalid')
                    for row in rows:
                        sku=row.get('sku') if isinstance(row,dict) else None
                        if sku not in batch or sku in seen:raise ValueError('goods_sales_identity_invalid')
                        seen.add(sku)
                        storages=row.get('storageSalesList')
                        record={'sku':sku,'main_sku':row.get('mainSku'),'observed_at':now(),
                            'status':'ok','warehouses':[]}
                        if not isinstance(storages,list) or not storages:
                            record['status']='unavailable'
                        else:
                            identities=set()
                            for storage in storages:
                                if not isinstance(storage,dict):record['status']='invalid_response';continue
                                identity=(storage.get('storageEntityName'),storage.get('storageName'))
                                quantity=storage.get('sevenDays')
                                valid=(storage.get('sku')==sku and all(isinstance(x,str) and x for x in identity)
                                    and identity not in identities and type(quantity) is int and quantity>=0)
                                identities.add(identity)
                                record['warehouses'].append({'storage_entity_name':identity[0],
                                    'storage_name':identity[1],'quantity':quantity if valid else None,
                                    'status':'ok' if valid else 'invalid_response'})
                                if not valid:record['status']='invalid_response'
                        result['records'][sku]=record
                    if len(seen)>total or more!=(len(seen)<total):raise ValueError('goods_sales_pagination_invalid')
                    if not more:break
                    page+=1
                except (httpx.HTTPError,ValueError,TypeError) as error:
                    result['issues'].append({'code':'goods_sales_read_failed','error_kind':type(error).__name__,'skus':batch})
                    for sku in batch:result['records'][sku]={'sku':sku,'status':'unavailable','warehouses':[]}
                    break
    except (httpx.HTTPError,ValueError,TypeError) as error:
        result['issues'].append({'code':'goods_sales_unavailable','error_kind':type(error).__name__})
    finally:
        private_request.reset(context)
        if owned:client.close()
    for sku in requested:
        result['records'].setdefault(sku,{'sku':sku,'status':'not_found','warehouses':[]})
    result['acquisition_complete']=all(v['status']=='ok' for v in result['records'].values()) and not result['issues']
    result['completed_at']=now()
    write_verified(result['artifact_file'],result)
    return result
