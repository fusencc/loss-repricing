"""复用销售批量导出，批查父商品；仅海外仓目标补充分仓成本明细。"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
import httpx
from app.adapters.normalization import money
from app.adapters.sales_auth import ORIGIN, SalesAuth
from app.adapters.sales_capture import _json_values
from app.adapters.sales_platform_capture import _archive, ACTIVITY_PATH
from app.adapters.base import SourceUnavailable
from app.services.erp_capture_store import write_json


def capture_batch_controls(settings, capture, targets, *, sales, client=None, auth=None, progress=None,
                           variant_cache=None):
    plans=defaultdict(set)
    for target in targets:
        plans[target['erp_shop'],target['product_id']].add(target['item_id'])
    exported=defaultdict(list)
    for row in sales['variants']:
        if row['sellerSkuId'] in plans.get((row['shopName'],row['productId']),set()):
            exported[row['shopName'],row['productId']].append(row)
    source_id='sales-batch-controls-'+uuid4().hex
    directory=Path(settings.runtime_dir)/'sales-platform-controls'/source_id
    directory.mkdir(parents=True,mode=0o700)
    now=lambda:datetime.now(timezone.utc).isoformat()
    result={'schema_version':'sales-platform-controls-v1','source_id':source_id,
        'source_mode':'sales_batch_export_with_parent_list','export_source_id':sales['source_id'],
        'erp_capture_id':capture['capture_id'],'synthetic':capture.get('synthetic',False),
        'sample_only':capture.get('sample_only',False),'queried_at':now(),'records':[],
        'requested':[{'erp_shop':s,'product_id':p,'item_ids':sorted(v)} for (s,p),v in plans.items()],
        'files':[],'issues':[],'pricing_ready':False,'artifact_file':str(directory/'result.json')}
    owned=client is None
    client=client or httpx.Client(timeout=60,trust_env=False)
    auth=auth or SalesAuth(settings,client)
    parents=defaultdict(list)
    ids=sorted({p for s,p in plans})
    try:
        for offset in range(0,len(ids),50):
            batch=ids[offset:offset+50];page=1
            if progress:progress(f'批量读取父商品：{offset+1}-{min(offset+50,len(ids))}/{len(ids)}。')
            while True:
                body={'page':page,'limit':100,'blocNames':[settings.erp_capture_bloc],'productIdStr':' '.join(batch)}
                response=client.post(ORIGIN+ACTIVITY_PATH,json=body,headers=auth.collection_headers(),timeout=60)
                auth.observe_collection_response(response)
                observed=_archive(directory,result,response,f'parents-{offset:05d}-{page}.json',ACTIVITY_PATH,body)
                value=response.json(parse_float=Decimal)
                if value.get('code')!=0:raise SourceUnavailable('批量父商品列表读取失败')
                rows=value['data']
                for row in rows:
                    key=row.get('shopName'),row.get('productId')
                    if key in plans and row.get('bloc')==settings.erp_capture_bloc:parents[key].append((row,observed))
                if len(rows)<100 or page*100>=value['count']:break
                page+=1
        for number,(key,items) in enumerate(plans.items(),1):
            shop,product=key
            try:
                parent,parent_observed=parents[key][0] if len(parents[key])==1 else (None,None)
                if parent is None:raise ValueError('批量父商品归属不唯一')
                rows=exported[key]
                if len(rows)!=len(items) or {r['sellerSkuId'] for r in rows}!=items:
                    raise ValueError('批量导出子商品身份不唯一')
                overseas=any(money(r['batch_pricing']['海外仓库存'])>0 for r in rows
                             if r['batch_pricing']['海外仓库存'] not in (None,''))
                details={}
                if overseas:
                    cached=(variant_cache or {}).get(key)
                    if cached is not None:
                        raw=cached['rows'];result.setdefault('reused_variant_files',[]).append(cached['file'])
                    else:
                        if progress:progress(f'补充海外仓成本明细：产品{number}/{len(plans)}。')
                        path='/Api/api-tiktok-sales/online/findProductVariants/'+quote(shop,safe='')+'/'+quote(product,safe='')
                        body={'shopName':shop,'productId':product}
                        response=client.post(ORIGIN+path,json=body,headers=auth.collection_headers(),timeout=60)
                        auth.observe_collection_response(response)
                        _archive(directory,result,response,f'{number:05d}-variants.json',path,body)
                        value=response.json(parse_float=Decimal)
                        if value.get('resp_code')!=0:raise ValueError('海外仓明细读取失败')
                        raw=value['datas']
                    for row in raw:
                        if (row.get('shopName'),row.get('productId'))==key:
                            details[row['sellerSkuId']]=row
                variants=[];skus=[];active=False
                for row in rows:
                    values=row['batch_pricing'];stock=values['海外仓库存'];item=row['sellerSkuId']
                    detail=details.get(item,{})
                    if stock not in (None,'') and money(stock)>0 and not detail:
                        raise ValueError('海外仓子商品缺少分仓成本明细')
                    variant={**detail,**{k:row[k] for k in ('shopName','shopId','productId','sellerSkuId','sellerSku','localSellerSku')},
                        'currency':values['币种'],'priceIncludeVat':values['含vat价格'],'marketPrice':values['营销价格'],
                        'discount':values['折扣'],'overseasStockSum':stock,'costPrice':values['erp成本价'],'localWeight':values['包装重量']}
                    variants.append(variant)
                    skus.append({'id':item,'seller_sku':row['sellerSku'],'price':{'sale_price':values['含vat价格'],'currency':values['币种']}})
                    active=active or bool(values['活动类型'])
                result['records'].append({'erp_shop':shop,'product_id':product,'item_ids':sorted(items),
                    'company_parent':_json_values(parent),'company_activity_locked':active,
                    'current_variants':_json_values(variants),'data':{'id':product,'skus':skus},
                    'observed_at':sales['completed_at'],'parent_observed_at':parent_observed,'ref':f'{source_id}/{number}'})
            except (ValueError,KeyError,TypeError,httpx.HTTPError) as error:
                result['issues'].append({'code':'batch_online_components_missing','erp_shop':shop,'product_id':product,'reason':str(error) if isinstance(error,ValueError) else type(error).__name__})
            result['completed_at']=now()
            write_json(result['artifact_file'],result)
    finally:
        if owned:client.close()
    result.update(completed_at=now(),acquisition_complete=len(result['records'])==len(plans) and not result['issues'])
    write_json(result['artifact_file'],result)
    return result
