"""公司日志与平台回执的确定性解码；不把公司完成时间当作实价生效时间。"""
from datetime import datetime,timezone
from fractions import Fraction
from decimal import Decimal
from hashlib import sha256
import json

from app.adapters.erp_pricing import MappedKey
from app.adapters.normalization import money,source_time
from app.domain.engine import BEIJING,SITE_CURRENCIES
from app.domain.types import VariantKey

AVAILABILITY_TYPES={4,5,10,13,14,16,23,27}
PRICE_FIELDS=('priceOld','priceOldIncludeVat','priceNew','priceIncludeVat',
    'overseaPriceExcludeVat','directPriceExcludeVat','overseaPriceIncludeVat','directPriceIncludeVat')
CONTEXT_FIELDS=('id','type','status','shopName','productId','sellerSkuId','completeTime','versionId',*PRICE_FIELDS)


def company_record_relevant(row,target):
    key=target['key']
    if row.get('shopName')!=target['erp_shop']:return False
    if row.get('productId') and row['productId']!=key['product_id']:
        return row.get('sellerSkuId')==key['item_id']
    return True


def _identity(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not str(value).strip():
        raise ValueError('公司回执身份缺失')
    return str(value)


def _positive(value):
    result=money(value)
    if result<=0:raise ValueError('公司新旧价格不是已知正数')
    return result


def _amount_key(value):
    ratio=Fraction(value)
    return [str(ratio.numerator),str(ratio.denominator)]


def classify_company_event(row,receipt,*,target,control):
    result={'log_id':str(row.get('id')),'classification':'unknown','reason':'公司任务影响或回执尚未核实',
        'event_at':None,'observation':None}
    if not company_record_relevant(row,target):
        result.update(classification='not_applicable',reason='属于其它店铺或产品');return result
    try:
        key=MappedKey.model_validate(target['key']);log_id=_identity(row['id'])
        event=datetime.strptime(row['completeTime'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING)
        result['event_at']=event.astimezone(timezone.utc).isoformat()
        if (receipt is None or receipt.get('requested_log_id')!=log_id
                or receipt.get('returned_log_id') not in (None,log_id)):
            raise ValueError('公司请求日志ID绑定不一致')
        if (control['erp_shop']!=target['erp_shop'] or control['product_id']!=key.product_id
                or control['data']['id']!=key.product_id):raise ValueError('当前平台产品绑定不一致')
        platform_seen=source_time(control['platform_price_observed_at'])
        variants_seen=source_time(control['variants_observed_at'])
        body,reply=receipt.get('request_body'),receipt.get('platform_response')
        if (type(row.get('status')) is not int or row['status']!=2 or not isinstance(body,dict)
                or not isinstance(reply,dict) or type(reply.get('code')) is not int or reply['code']!=0
                or not isinstance(reply.get('data'),dict)):
            raise ValueError('公司或平台回执尚未确认成功')
        skus=control['data']['skus']
        current=[item for item in skus if _identity(item.get('id'))==key.item_id]
        if len(current)!=1:raise ValueError('当前平台SKU不唯一')
        currency=SITE_CURRENCIES[key.site]
        if current[0]['price'].get('currency')!=currency:raise ValueError('当前平台币种不一致')
        if set(body)=={'product_ids'} and row.get('type') in AVAILABILITY_TYPES:
            ids=body['product_ids']
            if (not isinstance(ids,list) or len(ids)!=1 or _identity(ids[0])!=key.product_id
                    or row.get('productId')!=key.product_id
                    or any(name not in row or row[name] is not None for name in PRICE_FIELDS)):
                raise ValueError('上下架请求范围或价格空值未证实')
            if event>min(platform_seen,variants_seen):
                result.update(classification='snapshot_stale',reason='上下架事件晚于当前快照，须重取当前状态');return result
            result.update(classification='availability_only',reason='已核对仅上下架或删除，不包含价格操作');return result
        if set(body)!={'skus'} or not isinstance(body['skus'],list) or not body['skus']:
            raise ValueError('公司价格请求形状未知')
        requested={}
        for item in body['skus']:
            if not isinstance(item,dict) or set(item)!={'id','price'}:
                raise ValueError('公司SKU请求字段未知')
            item_id=_identity(item['id']);price=item['price']
            if item_id in requested:raise ValueError('公司SKU请求身份重复')
            if not isinstance(price,dict) or set(price)!={'amount','currency'} or price['currency']!=currency:
                raise ValueError('请求币种或价格字段未知')
            _positive(price['amount']);requested[item_id]=item
        parent_id=_identity(row.get('sellerSkuId'))
        if parent_id not in requested:raise ValueError('请求SKU与父日志身份不一致')
        item=requested[parent_id]
        if parent_id!=key.item_id:
            if key.item_id in requested:
                if event>min(platform_seen,variants_seen):
                    result.update(classification='snapshot_stale',reason='批量请求也修改目标SKU且晚于快照');return result
                raise ValueError('批量请求含目标SKU，但当前父日志未提供其旧价')
            if row.get('productId')==key.product_id:
                result.update(classification='not_applicable',reason='已核对属于同产品其它SKU');return result
            raise ValueError('请求SKU与父日志身份不一致')
        if row.get('sellerSkuId')!=key.item_id or row.get('productId') not in (None,key.product_id):
            raise ValueError('父日志与当前SKU归属不一致')
        price=item['price']
        if not isinstance(price,dict) or set(price)!={'amount','currency'} or price['currency']!=currency:
            raise ValueError('请求币种或价格字段未知')
        before,after=_positive(row.get('priceOld')),_positive(row.get('priceNew'))
        if _positive(price['amount'])!=after:raise ValueError('父日志新税前价与平台请求金额不一致')
        stale=event>min(platform_seen,variants_seen)
        if stale and (after<=before or receipt.get('received_at') is None):
            result.update(classification='snapshot_stale',reason='价格事件晚于当前价格，须重取平台与营销价');return result
        if after<=before:
            result.update(classification='non_increase',reason='同一税前口径下降或持平');return result
        observed=current[0]['price'].get('tax_exclusive_price')
        first_seen=source_time(receipt['received_at']) if stale else platform_seen
        if first_seen<event:raise ValueError('回执观察早于公司完成时间')
        confirmed=not stale and observed is not None and _positive(observed)==after
        state='confirmed' if confirmed else 'unverified'
        identity=['company_operation_log',log_id,key.target_identity(),result['event_at'],currency,
            _amount_key(before),_amount_key(after)]
        event_key=sha256(json.dumps(identity,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        result['observation']={'event_key':event_key,'log_id':log_id,'variant_id':key.target_identity(),
            'key':key.model_dump(mode='json'),'state':state,'currency':currency,
            'old_price':format(before,'f'),'new_price':format(after,'f'),
            'first_observed_at':first_seen.astimezone(timezone.utc).isoformat(),
            'first_confirmed_at':platform_seen.astimezone(timezone.utc).isoformat() if confirmed else None,
            'logged_complete_at':result['event_at'],
            'company_context':{name:row.get(name) for name in CONTEXT_FIELDS},
            'evidence':{'receipt_ref':receipt['ref'],'actual_price_basis':'tax_exclusive',
                'actual_tax_exclusive_price':str(observed) if observed is not None else None,
                'platform_observed_at':platform_seen.isoformat(),'variants_observed_at':variants_seen.isoformat()}}
        result.update(classification='snapshot_stale' if stale else 'confirmed_increase' if confirmed else 'unverified_increase',
            reason='平台实价匹配已接受的新税前价' if confirmed else '已接受涨价但当前实价未匹配，保持待核验')
    except (ValueError,TypeError,KeyError,ArithmeticError) as error:
        result['reason']=str(error)
    return result


def _decode_business_json(value):
    if isinstance(value,str):
        try:return json.loads(value,parse_float=Decimal)
        except ValueError:return None
    return value


def _redacted(value):
    import re
    if isinstance(value,dict):
        return {key:('[REDACTED]' if re.search(r'token|password|secret|authorization|signature',key,re.I)
            else _redacted(item)) for key,item in value.items()}
    if isinstance(value,list):return [_redacted(item) for item in value]
    if isinstance(value,str) and re.search(r'access.?token|authorization|client.?secret|password',value,re.I):
        return '[REDACTED_AUTH_TEXT]'
    return value


def capture_company_history_effects(settings,capture,targets,company,platform,prior_history,*,client=None,progress=None):
    """读确切日志ID的请求/回执，分类影响并生成待登记观察；不调用任何改价接口。"""
    from pathlib import Path
    from uuid import uuid4
    import httpx
    from app.adapters.base import SourceUnavailable
    from app.adapters.erp_order_loss import _bound
    from app.adapters.sales_auth import SalesAuth,ORIGIN
    from app.adapters.sales_capture import _json_values
    from app.adapters.transport_privacy import private_request
    from app.services.erp_capture_store import write_verified
    if settings.vendor_api_url.rstrip('/')!=ORIGIN:raise SourceUnavailable('公司回执来源地址未确认')
    capture=dict(capture,synthetic=capture.get('synthetic',False),sample_only=capture.get('sample_only',False))
    for value,schema in ((company,'sales-company-price-history-v1'),(platform,'sales-platform-controls-v1'),
                         (prior_history,'local-live-price-history-v1')):_bound(capture,value,schema)
    source_id='company-history-effects-'+uuid4().hex
    directory=Path(settings.runtime_dir)/'company-history-effects'/source_id;directory.mkdir(mode=0o700,parents=True)
    result={'schema_version':'company-price-effects-v1','source_id':source_id,
        'erp_capture_id':capture['capture_id'],'synthetic':capture['synthetic'],'sample_only':capture['sample_only'],
        'company_history_source_id':company['source_id'],'platform_source_id':platform['source_id'],
        'prior_local_history_source_id':prior_history['source_id'],
        'queried_at':datetime.now(timezone.utc).isoformat(),'requested_log_ids':[],
        'receipts':{},'effects':[],'observations':[],'issues':[],'files':[],'pricing_ready':False,
        'artifact_file':str((directory/'result.json').resolve())}
    contexts=[];seen_contexts=set();context_sources={}
    def add_context(row,origin):
        if not any(company_record_relevant(row,target) for target in targets):return
        value={name:row.get(name) for name in CONTEXT_FIELDS}
        token=json.dumps(value,sort_keys=True,ensure_ascii=False)
        if token not in seen_contexts:
            contexts.append(value);seen_contexts.add(token);context_sources[token]=origin
    for row in company['records']:add_context(row,company['source_id'])
    for row in prior_history['company_observations']:
        if row['state']=='unverified' and any(
                VariantKey.model_validate(row['key']).target_identity()==MappedKey.model_validate(target['key']).target_identity()
                for target in targets):add_context(row['company_context'],prior_history['source_id'])
    lookup=sorted({_identity(row['id']) for row in contexts})
    result['requested_log_ids']=lookup
    owned=client is None;client=client or httpx.Client(timeout=60,trust_env=False,follow_redirects=False)
    auth=SalesAuth(settings,client);context=private_request.set(True)
    try:
        for number,log_id in enumerate(lookup,1):
            stage='authentication'
            try:
                token=auth.get_token();stage='request'
                if progress:progress(f'核对公司任务实际请求与回执：第{number}/{len(lookup)}条。')
                response=client.get(ORIGIN+'/Api/api-tiktok-sales/operationLog/getRequestAndResponseById',
                    params={'id':log_id},headers={'Authorization':'Bearer '+token},timeout=60,follow_redirects=False)
                raw=response.content
                try:payload=response.json(parse_float=Decimal)
                except ValueError:payload={'invalid_json_response':True}
                data=payload.get('datas') if isinstance(payload,dict) else None
                if isinstance(data,dict):
                    data=dict(data)
                    for field in ('requestBody','responseResult'):data[field]=_decode_business_json(data.get(field))
                    payload={**payload,'datas':data}
                clean=_json_values(_redacted(payload));file=directory/f'{number:05d}.json';write_verified(file,clean)
                saved=file.read_bytes()
                received=datetime.now(timezone.utc).isoformat()
                result['files'].append({'file':str(file.resolve()),'bytes':len(saved),'sha256':sha256(saved).hexdigest(),
                    'raw_response_sha256':sha256(raw).hexdigest(),'raw_response_bytes':len(raw),
                    'projection':'credential_redacted','request':{'id':log_id},'http_status':response.status_code,
                    'received_at':received})
                response.raise_for_status()
                if (not isinstance(clean,dict) or type(clean.get('resp_code')) is not int or clean['resp_code']!=0
                        or not isinstance(clean.get('datas'),dict)):
                    raise ValueError('company_receipt_response_unverified')
                data=clean['datas']
                result['receipts'][log_id]={'requested_log_id':log_id,'returned_log_id':data.get('id'),
                    'request_body':data.get('requestBody'),'platform_response':data.get('responseResult'),
                    'received_at':received,'ref':f'{source_id}/receipt/{log_id}'}
            except (SourceUnavailable,httpx.HTTPError,ValueError) as error:
                result['issues'].append({'code':'company_receipt_unavailable','log_id':log_id,'error_kind':type(error).__name__})
                if stage=='authentication' or (isinstance(error,httpx.HTTPStatusError)
                        and error.response.status_code in (401,403)):break
    finally:
        private_request.reset(context)
        if owned:client.close()
    controls={}
    for row in platform['records']:controls.setdefault((row['erp_shop'],row['product_id']),[]).append(row)
    prior_confirmed={row['event_key']:row for row in prior_history['company_observations'] if row['state']=='confirmed'}
    observations={};seen_targets=set()
    for target in targets:
        key=MappedKey.model_validate(target['key'])
        if key.identity() in seen_targets:continue
        seen_targets.add(key.identity())
        effect={'key':key.model_dump(mode='json'),'erp_shop':target['erp_shop'],
            'history_known':company.get('company_log_complete') is True and target['erp_shop'] in company['requested_shops'],
            'pending_submission':False,'snapshot_stale':False,'confirmed_event_keys':[],'unverified_event_keys':[],
            'events':[]}
        matching=controls.get((target['erp_shop'],key.product_id),[])
        if len(matching)!=1:effect['history_known']=False
        for row in contexts:
            if not company_record_relevant(row,target):continue
            if len(matching)!=1:continue
            classified=classify_company_event(row,result['receipts'].get(str(row['id'])),target=target,control=matching[0])
            proposal=classified.pop('observation')
            if proposal is not None and proposal['event_key'] in prior_confirmed:
                effect['confirmed_event_keys'].append(proposal['event_key'])
                classified.update(classification='previously_confirmed_increase',event_key=proposal['event_key'],
                    reason='权威档案已保存首次实际确认，后续价格变化不撤销原提价保护')
                proposal=None
            if proposal is not None:
                proposal['evidence'].update(company_history_source=company['source_id'],platform_source=platform['source_id'],
                    effects_source=source_id,company_context_source=context_sources[json.dumps(row,sort_keys=True,ensure_ascii=False)])
                observations.setdefault(proposal['event_key'],proposal)
                classified['event_key']=proposal['event_key']
                field='confirmed_event_keys' if proposal['state']=='confirmed' else 'unverified_event_keys'
                effect[field].append(proposal['event_key'])
            kind=classified['classification']
            if kind in ('unknown','snapshot_stale','unverified_increase'):effect['history_known']=False
            if kind=='snapshot_stale':effect['snapshot_stale']=True
            if kind=='unverified_increase':effect['pending_submission']=True
            effect['events'].append(classified)
        result['effects'].append(effect)
    result['observations']=list(observations.values())
    result['acquisition_complete']=not result['issues'] and len(result['receipts'])==len(lookup)
    result['completed_at']=datetime.now(timezone.utc).isoformat()
    write_verified(result['artifact_file'],result)
    return result
