"""逐店铺产品读取平台当前身份/可售/价格证据；未知字段保留未知，不映射默认放行。"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from urllib.parse import quote
import json
import httpx
from app.adapters.base import SourceUnavailable
from app.adapters.normalization import identity_text
from app.adapters.sales_auth import SalesAuth, ORIGIN
from app.adapters.sales_capture import _json_values
from app.adapters.transport_privacy import private_request
from app.services.erp_capture_store import write_verified

PATH = '/Api/api-tiktok-sales/online/getEditOnlineDataByShopNameAndProductId'


def capture_platform_controls(settings,capture,targets,*,client=None,progress=None):
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('平台现态来源地址未确认')
    plans=defaultdict(set)
    for target in targets:
        shop,product,item=(identity_text(target[key]) for key in ('erp_shop','product_id','item_id'))
        plans[shop,product].add(item)
    source_id='sales-platform-controls-'+uuid4().hex
    directory=Path(settings.runtime_dir)/'sales-platform-controls'/source_id
    directory.mkdir(mode=0o700,parents=True)
    result={'schema_version':'sales-platform-controls-v1','source_id':source_id,
        'erp_capture_id':capture['capture_id'],'synthetic':bool(capture.get('synthetic',False)),
        'sample_only':bool(capture.get('sample_only',False)),'queried_at':datetime.now(timezone.utc).isoformat(),
        'requested':[{'erp_shop':shop,'product_id':product,'item_ids':sorted(items)}
            for (shop,product),items in sorted(plans.items())],
        'records':[],'files':[],'issues':[],'pricing_ready':False,
        'artifact_file':str((directory/'result.json').resolve())}
    owned=client is None
    client=client or httpx.Client(timeout=60,trust_env=False,follow_redirects=False)
    auth=SalesAuth(settings,client)
    context=private_request.set(True)
    try:
        for number,((shop,product),items) in enumerate(sorted(plans.items()),1):
            if progress:progress(f'读取平台当前商品身份与可售状态：第{number}/{len(plans)}个产品。')
            stage='authentication'
            try:
                token=auth.get_token()
                stage='request'
                response=client.get(ORIGIN+PATH,params={'shopName':shop,'productId':product},
                    headers={'Authorization':'Bearer '+token},timeout=60,follow_redirects=False)
                path=directory/f'{number:05d}.json';path.write_bytes(response.content)
                raw=path.read_bytes()
                if raw!=response.content:raise OSError('平台现态原件回读不一致')
                received=datetime.now(timezone.utc).isoformat()
                result['files'].append({'file':str(path.resolve()),'bytes':len(raw),'sha256':sha256(raw).hexdigest(),
                    'path':PATH,'request':{'shopName':shop,'productId':product},
                    'http_status':response.status_code,'received_at':received})
                response.raise_for_status()
                if not 200<=response.status_code<300:raise ValueError('platform_response_redirect')
                value=response.json(parse_float=Decimal)
                if not isinstance(value,dict) or type(value.get('resp_code')) is not int or value['resp_code']!=0:
                    raise ValueError('platform_business_failed')
                data=value.get('datas')
                if not isinstance(data,dict) or data.get('id')!=product or not isinstance(data.get('skus'),list):
                    raise ValueError('platform_product_scope_mismatch')
                skus=data['skus']
                ids=[identity_text(sku['id']) for sku in skus if isinstance(sku,dict)]
                if len(ids)!=len(skus) or len(ids)!=len(set(ids)) or not items<=set(ids):
                    raise ValueError('platform_sku_scope_mismatch')
                # 原件保留，规范来源仅投影本流程所需业务字段，缺字段不补默认值。
                projected={key:data.get(key) for key in ('id','status','product_status','is_not_for_sale','update_time')}
                projected['skus']=[{key:sku.get(key) for key in ('id','seller_sku','price','status_info','inventory')}
                    for sku in skus]
                # 当前前端isPromotion=0/1为明确是否参加活动。两种筛选都读，
                # 只有唯一一边明确返回同产品时才确认，不能以null活动字段猜无活动。
                matches={}
                for active in (0,1):
                    body={'page':1,'limit':100,'blocNames':[settings.erp_capture_bloc],
                        'productIdStr':product,'isPromotion':'0' if active==0 else 1}
                    activity_response=client.post(ORIGIN+'/Api/api-tiktok-sales/online/findByPage',json=body,
                        headers={'Authorization':'Bearer '+token},timeout=60,follow_redirects=False)
                    path=directory/f'{number:05d}-activity-{active}.json'
                    path.write_bytes(activity_response.content);raw=path.read_bytes()
                    if raw!=activity_response.content:raise OSError('活动状态原件回读不一致')
                    result['files'].append({'file':str(path.resolve()),'bytes':len(raw),'sha256':sha256(raw).hexdigest(),
                        'path':'/Api/api-tiktok-sales/online/findByPage','request':body,
                        'http_status':activity_response.status_code,'received_at':datetime.now(timezone.utc).isoformat()})
                    activity_response.raise_for_status()
                    value=activity_response.json(parse_float=Decimal)
                    if (not isinstance(value,dict) or type(value.get('code')) is not int or value['code']!=0
                            or type(value.get('count')) is not int or value['count'] not in (0,1)
                            or not isinstance(value.get('data'),list) or len(value['data'])!=value['count']):
                        raise ValueError('company_activity_coverage_unverified')
                    rows=value['data']
                    if any(not isinstance(row,dict) or row.get('shopName')!=shop or row.get('productId')!=product
                            or row.get('bloc')!=settings.erp_capture_bloc for row in rows):
                        raise ValueError('company_activity_scope_mismatch')
                    if rows:matches[active]=rows[0]
                if len(matches)!=1:raise ValueError('company_activity_state_conflicting_or_missing')
                active,parent=next(iter(matches.items()))
                variant_path='/Api/api-tiktok-sales/online/findProductVariants/'+quote(shop,safe='')+'/'+quote(product,safe='')
                variant_body={'shopName':shop,'productId':product}
                variant_response=client.post(ORIGIN+variant_path,json=variant_body,
                    headers={'Authorization':'Bearer '+token},timeout=60,follow_redirects=False)
                path=directory/f'{number:05d}-variants.json';path.write_bytes(variant_response.content);raw=path.read_bytes()
                if raw!=variant_response.content:raise OSError('当前变体原件回读不一致')
                result['files'].append({'file':str(path.resolve()),'bytes':len(raw),'sha256':sha256(raw).hexdigest(),
                    'path':variant_path,'request':variant_body,'http_status':variant_response.status_code,
                    'received_at':datetime.now(timezone.utc).isoformat()})
                variants_observed=result['files'][-1]['received_at']
                variant_response.raise_for_status()
                value=variant_response.json(parse_float=Decimal)
                if not isinstance(value,dict) or type(value.get('resp_code')) is not int or value['resp_code']!=0:
                    raise ValueError('current_variant_business_failed')
                variants=value.get('datas')
                if (not isinstance(variants,list) or any(not isinstance(row,dict) or row.get('shopName')!=shop
                        or row.get('productId')!=product for row in variants)):
                    raise ValueError('current_variant_scope_mismatch')
                ids=[identity_text(row.get('sellerSkuId')) for row in variants]
                if len(ids)!=len(set(ids)) or not items<=set(ids):
                    raise ValueError('current_variant_identity_ambiguous')
                completed=datetime.now(timezone.utc).isoformat()
                result['records'].append({'platform_price_observed_at':received,'variants_observed_at':variants_observed,'company_activity_locked':bool(active),'company_parent':_json_values(parent),
                    'current_variants':_json_values(variants),
                    'erp_shop':shop,'product_id':product,'item_ids':sorted(items),
                    'observed_at':completed,'data':_json_values(projected),'ref':f'{source_id}/{number}'})
            except (SourceUnavailable,httpx.HTTPError,ValueError,TypeError,KeyError) as error:
                result['issues'].append({'code':'platform_current_state_unverified','erp_shop':shop,
                    'product_id':product,'item_ids':sorted(items),'error_kind':type(error).__name__})
                if stage=='authentication' or (isinstance(error,httpx.HTTPStatusError)
                        and error.response.status_code in (401,403)):
                    break
    finally:
        private_request.reset(context)
        if owned:client.close()
    result['acquisition_complete']=len(result['records'])==len(plans) and not result['issues']
    result['completed_at']=datetime.now(timezone.utc).isoformat()
    write_verified(result['artifact_file'],result)
    return result
