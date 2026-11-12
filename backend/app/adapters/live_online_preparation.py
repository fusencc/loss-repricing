"""已绑定真实来源的在线输入准备；仅复用统一费用模型和规则，不计算第二套建议价。

无归属/销量/保护证据不生成乐观状态。公司日志尚未分类的影响保留history_known=False。
站点步长和向上取整来自2026-11-11用户明确确认；税后价格列与当前模板核对。
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from app.adapters.erp_order_loss import _bound
from app.adapters.erp_pricing import LiveOnlinePreparation, MappedKey, ReviewedOnlineState
from app.adapters.normalization import money, source_time
from app.adapters.sales_fee_model import compile_overseas_fee
from app.domain.engine import BEIJING, SITE_CURRENCIES, sales_window
from app.domain.types import VariantKey

SCHEMAS={'sales':'sales-source-observations-v1','platform':'sales-platform-controls-v1',
    'window':'erp-complete-sales-window-v1','company_history':'sales-company-price-history-v1',
    'local_history':'local-live-price-history-v1','company_effects':'company-price-effects-v1'}


def prepare_online_states(capture,targets,*,sales,platform,window,company_history,local_history,company_effects):
    capture=dict(capture,synthetic=capture.get('synthetic',False),sample_only=capture.get('sample_only',False))
    if type(capture['synthetic']) is not bool or type(capture['sample_only']) is not bool:
        raise ValueError('采集环境标记无效')
    sources=locals().copy()
    for name,schema in SCHEMAS.items():_bound(capture,sources[name],schema)
    if (company_effects['company_history_source_id']!=company_history['source_id']
            or company_effects['platform_source_id']!=platform['source_id']):
        raise ValueError('公司任务影响与历史或当前平台来源不一致')
    observed=max(source_time(sources[name]['completed_at']) for name in SCHEMAS)
    latest_price=max([source_time(sales['completed_at']),source_time(window['completed_at']),
        *(source_time(row['observed_at']) for row in platform['records'])])
    history_coverage=False
    evaluated=latest_price
    try:
        history_start=source_time(company_history['window_start'])
        history_end=source_time(company_history['window_end'])
        if history_end<=source_time(company_history['queried_at']) and history_end<=observed:
            evaluated=max(latest_price,history_end)
            history_coverage=history_end>=latest_price and history_start<=history_end-timedelta(hours=72)
    except (ValueError,TypeError,KeyError):
        pass
    result={'schema_version':'erp-live-online-preparation-v1','version':'erp-live-online-v1',
        'erp_capture_id':capture['capture_id'],'synthetic':capture['synthetic'],'sample_only':capture['sample_only'],
        'pricing_ready':False,'observed_at':observed.isoformat(),'evaluated_at':evaluated.isoformat(),'online':[],'contracts':[],'issues':[],
        'references':{name:source['source_id'] for name,source in sources.items() if name in SCHEMAS}}
    result['references']['proofs']={}
    controls=defaultdict(list)
    for row in platform['records']:controls[row['erp_shop'],row['product_id']].append(row)
    volumes=defaultdict(list)
    for row in window['volumes']:volumes[row['erp_shop'],row['main_sku']].append(row)
    start,end=sales_window(source_time(capture['queried_at']))
    if window['window_start']!=str(start) or window['window_end']!=str(end):
        raise ValueError('在线准备的销量窗口与本次业务日不一致')
    # 同一平台SKU可能来自不同历史订单仓；先收齐定义再判断，不能保留先到的一条。
    # 冲突只挂起该SKU，其他独立目标继续，符合项目的异常范围约束。
    target_groups=defaultdict(list)
    for target in targets:
        identity=MappedKey.model_validate(target['key']).identity()
        if target not in target_groups[identity]:target_groups[identity].append(target)
    for definitions in target_groups.values():
        if len(definitions)>1:
            for target in definitions:
                result['issues'].append({'code':'live_online_state_pending',
                    'reason':'同一在线身份的准备定义冲突，全部关联定义待处理',
                    'target':MappedKey.model_validate(target['key']).model_dump(mode='json')})
            continue
        target=definitions[0]
        key=MappedKey.model_validate(target['key'])
        try:
            rows=controls[target['erp_shop'],key.product_id]
            if len(rows)!=1:raise ValueError('缺少唯一的当前平台来源')
            control=rows[0];parent=control['company_parent'];data=control['data']
            if (parent.get('shopName')!=target['erp_shop'] or parent.get('shopId')!=key.shop_id
                    or parent.get('productId')!=key.product_id or parent.get('localMainSku')!=target['main_sku']
                    or data.get('id')!=key.product_id):
                raise ValueError('当前平台、销售父商品与候选归属不一致')
            variants=[row for row in control['current_variants'] if row.get('sellerSkuId')==key.item_id]
            skus=[row for row in data['skus'] if row.get('id')==key.item_id]
            if len(variants)!=1 or len(skus)!=1:raise ValueError('当前子商品身份不唯一')
            variant,sku=variants[0],skus[0]
            if (variant.get('shopName')!=target['erp_shop'] or variant.get('shopId')!=key.shop_id
                    or variant.get('productId')!=key.product_id or variant.get('sellerSku')!=key.sku
                    or variant.get('localSellerSku')!=target['erp_sku'] or sku.get('seller_sku')!=key.sku):
                raise ValueError('当前平台及ERP子SKU不一致')
            currency=SITE_CURRENCIES[key.site];price=sku['price']
            retail=money(price['sale_price'])
            if (price.get('currency')!=currency or variant.get('currency')!=currency or retail<=0
                    or retail!=money(variant.get('priceIncludeVat'))):
                raise ValueError('当前含VAT价格、币种与公司快照不一致')
            actual=money(variant.get('marketPrice'))
            if not Decimal(0)<actual<=retail:raise ValueError('当前营销价格或折扣无效')
            window_rows=volumes[target['erp_shop'],target['main_sku']]
            if (len(window_rows)!=1 or window_rows[0].get('complete') is not True
                    or type(window_rows[0].get('quantity')) is not int or window_rows[0]['quantity']<0):
                raise ValueError('七日销量窗口不完整，不填0')
            allowed_binding={'configured_live_history_database','live_runtime_database'}
            if capture['synthetic']:allowed_binding.add('injected_synthetic_test')
            if (local_history.get('acquisition_complete') is not True
                    or local_history.get('archive_binding') not in allowed_binding):
                raise ValueError('本程序人工保护与执行档案未取全')
            rates=[row for row in sales['overseas_rates'] if row.get('siteCode')==key.site]
            if (len(rates)!=1 or rates[0].get('authType')!=1 or rates[0].get('blocName')!=parent.get('bloc')
                    or any(row.get('stage')=='overseas_rate' for row in sales.get('issues',[]))):
                raise ValueError('海外仓费用层级不能唯一确认，不能越过未知覆盖项')
            quotes=[row['rate'] for row in sales['currency_rates'] if row.get('name')==currency]
            cny=[row['rate'] for row in sales['currency_rates'] if row.get('name')=='CNY']
            if (sales.get('currency_rate_unit')!='cny_per_currency' or len(quotes)!=1
                    or len(cny)!=1 or money(cny[0])!=1):raise ValueError('公司费用报价单位或基准未确认')
            fee=compile_overseas_fee(variant,rates[0],site=key.site,warehouse=key.warehouse,
                company_cny_per_currency=quotes[0])
            if (type(control.get('company_activity_locked')) is not bool
                    or type(data.get('is_not_for_sale')) is not bool
                    or not isinstance(data.get('product_status'),str) or not isinstance(data.get('status'),str)
                    or not isinstance(sku.get('status_info'),dict) or not isinstance(sku['status_info'].get('status'),str)
                    or type(parent.get('productStatus')) is not int):
                raise ValueError('平台可售或公司活动状态字段未知')
            sellable=(parent['productStatus']==4 and data['product_status']=='ACTIVATE' and data['status']=='ACTIVATE'
                and data['is_not_for_sale'] is False and sku['status_info']['status']=='NORMAL')
            history_known=(history_coverage and company_history.get('company_log_complete') is True
                and target['erp_shop'] in company_history['requested_shops']
                and company_history.get('protection_scope')=='company_log_and_local_verified_archive'
                and company_history.get('platform_manual_policy')=='deferred_by_user_20261111')
            matched_effects=[row for row in company_effects['effects']
                if MappedKey.model_validate(row['key']).identity()==key.identity()]
            if len(matched_effects)!=1 or matched_effects[0]['erp_shop']!=target['erp_shop']:
                raise ValueError('公司任务影响缺少唯一的当前目标归属')
            effect=matched_effects[0]
            history_known=history_known and effect.get('history_known') is True and effect.get('snapshot_stale') is False
            pending=effect.get('pending_submission') is True
            last=None;verified_times=set()
            observations={row['event_key']:row for row in local_history['company_observations']}
            for event_key in effect['confirmed_event_keys']:
                if event_key not in observations or observations[event_key]['state']!='confirmed':
                    raise ValueError('公司实价确认尚未登记到权威档案')
            for row in observations.values():
                if VariantKey.model_validate(row['key']).target_identity()!=key.target_identity():continue
                if row['state']=='unverified':pending=True;continue
                if row['state']!='confirmed':raise ValueError('公司观察状态未知')
                effective=source_time(row['first_confirmed_at'])
                if effective>evaluated:
                    pending=True;history_known=False;continue
                last=max(last,effective) if last else effective
            for row in local_history['executions']:
                if VariantKey.model_validate(row['key']).target_identity()!=key.target_identity():continue
                if row['state']=='failed':continue
                if row['state']!='verified':pending=True;continue
                evidence=row['evidence']
                if (row.get('mode')!='live' or evidence.get('mode')!='live'
                        or evidence.get('price_basis')!='actual_including_vat' or money(evidence.get('actual_price'))<=0):
                    raise ValueError('本程序真实生效证据无效')
                effective=source_time(evidence['effective_at'])
                if effective>observed:raise ValueError('本程序生效时间晚于来源观察')
                if effective>evaluated:
                    history_known=False;pending=True;continue
                verified_times.add(effective)
                last=max(last,effective) if last else effective
            for guard in local_history['guards']:
                if guard['variant_id']!=key.target_identity():continue
                if guard.get('execution_id') is not None:
                    pending=True
                    continue
                if guard.get('last_effective_at') is not None:
                    effective=source_time(guard['last_effective_at'])
                    if effective>observed:raise ValueError('历史锁生效时间晚于来源观察')
                    if effective>evaluated:
                        history_known=False;pending=True
                    elif effective not in verified_times and evaluated-effective<timedelta(hours=72):
                        history_known=False

            protected=any(row['shop_id']==key.shop_id and row['site']==key.site and row['main_sku']==target['main_sku']
                for row in local_history['protections'])
            state=ReviewedOnlineState.model_validate({'key':key.model_dump(mode='json'),'main_sku':target['main_sku'],
                'currency':currency,'retail_price':format(retail,'f'),'marketing_price':format(actual,'f'),
                'current_margin':format(fee['current_margin'],'f'),'fee_scope':'erp-corrected-first-margin-v1',
                'fee_model':fee['model'].model_dump(mode='json'),'observed_at':control['observed_at'],
                'sales_7d':window_rows[0]['quantity'],'sales_start':str(start),'sales_end':str(end),'sales_complete':True,
                'history_known':history_known,'last_increase_at':last.isoformat() if last else None,
                'pending_submission':pending,'protected':protected,'activity_locked':control['company_activity_locked'],
                'sellable':sellable})
            result['online'].append({'ref':control['ref'],'state':state.model_dump(mode='json')})
            result['references']['proofs'][key.identity()]={'fee':fee['proof'],'company_events':effect['events'],
                'platform_observed_at':control.get('platform_price_observed_at'),
                'current_components_observed_at':control['observed_at'],'bundle_completed_at':observed.isoformat(),
                'company_history_cutoff':company_history.get('window_end'),'evaluated_at':evaluated.isoformat()}
        except (ValueError,TypeError,KeyError,ArithmeticError) as error:
            result['issues'].append({'code':'live_online_state_pending','reason':str(error),'target':key.model_dump(mode='json')})
    warehouses=defaultdict(set)
    for entry in result['online']:
        key=entry['state']['key'];warehouses[key['site']].add(key['warehouse'])
    for site,values in sorted(warehouses.items()):
        result['contracts'].append({'ref':'user-approved-20261111-ceiling-and-after-vat',
            'contract':{'site':site,'verified':True,'price_step':'100' if site=='VN' else '0.01',
                'rounding':'ROUND_UP','upload_column':'税后价格','version':'user-approved-ceiling-20261111-v1',
                'warehouses':sorted(values)}})
    return LiveOnlinePreparation.model_validate(result).model_dump(mode='json')
