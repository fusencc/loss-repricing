"""已绑定真实来源的在线输入准备；仅复用统一费用模型和规则，不计算第二套建议价。

72小时保护使用官方调价日志计数，本地档案仅用于未完成执行和人工保护。
站点步长和向上取整来自2026-11-11用户明确确认；税后价格列与当前模板核对。
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from app.adapters.erp_order_loss import _bound, _warehouse_matches
from app.adapters.erp_pricing import LiveOnlinePreparation, MappedKey, ReviewedOnlineState
from app.adapters.normalization import money, source_time
from app.adapters.sales_fee_model import compile_overseas_fee, compile_direct_fee
from app.adapters.current_pricing_fees import select_current_rate, SCHEMA as FEE_SCHEMA
from app.domain.engine import BEIJING, SITE_CURRENCIES
from app.domain.money import MoneyRatio
from app.domain.types import VariantKey
from app.adapters.official_price_logs import recent_price_log_for
from app.adapters.sales_price_reference import price_reference_for

SCHEMAS={'sales':'sales-source-observations-v1','platform':'sales-platform-controls-v1',
    'local_history':'local-live-price-history-v1',
    'fixed_fees':FEE_SCHEMA}


def marketing_price_matches_discount(
    retail: Decimal, actual: Decimal, discount_off: Decimal, currency: str,
) -> bool:
    # 营销实价可能保留原始精度或按币种显示。容许不足一个显示单位的尾差。
    factor = MoneyRatio(Decimal(1)) - MoneyRatio(discount_off, Decimal(100))
    display_unit = Decimal(1) if currency == 'VND' else Decimal('0.01')
    return (MoneyRatio(retail) * factor - MoneyRatio(actual)).absolute() < MoneyRatio(display_unit)


def prepare_online_states(capture,targets,*,sales,platform,local_history,fixed_fees,official_history,
                          price_references=None):
    capture=dict(capture,synthetic=capture.get('synthetic',False),sample_only=capture.get('sample_only',False))
    if type(capture['synthetic']) is not bool or type(capture['sample_only']) is not bool:
        raise ValueError('采集环境标记无效')
    if (official_history.get('schema_version')!='official-recent-price-v1'
            or official_history.get('basis')!='official_type_2_completion_time'
            or official_history.get('synthetic') is not capture['synthetic']):
        raise ValueError('开放调价日志来源合同或运行环境不一致')
    sources=locals().copy()
    for name,schema in SCHEMAS.items():_bound(capture,sources[name],schema)
    observed=max([source_time(sources[name]['completed_at']) for name in SCHEMAS]+[source_time(official_history['completed_at'])])
    evaluated=observed
    result={'schema_version':'erp-live-online-preparation-v2','version':'erp-live-online-v2',
        'erp_capture_id':capture['capture_id'],'synthetic':capture['synthetic'],'sample_only':capture['sample_only'],
        'pricing_ready':False,'observed_at':observed.isoformat(),'evaluated_at':evaluated.isoformat(),'online':[],'contracts':[],'issues':[],
        'references':{name:source['source_id'] for name,source in sources.items() if name in SCHEMAS}}
    result['references']['official_price_history'] = {name:official_history[name] for name in ('source_id','artifact_file','window_start','window_end')}
    result['references']['proofs']={}
    controls=defaultdict(list)
    for row in platform['records']:controls[row['erp_shop'],row['product_id']].append(row)
    exported = defaultdict(list)
    batch_export = sales.get('source_mode') == 'online_sku_batch_export'
    if batch_export:
        for variant in sales['variants']:
            exported[variant['shopName'], variant['productId'], variant['sellerSkuId']].append(variant)
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
            raw_actual = variant.get('marketPrice')
            export_proof = None
            discount_off = None
            if batch_export:
                exported_rows = exported[target['erp_shop'], key.product_id, key.item_id]
                if len(exported_rows) != 1 or not _warehouse_matches(variant, key.warehouse):
                    raise ValueError('批量商品匹配不唯一或当前仓配与 ERP 不一致')
                exported_row = exported_rows[0]
                export_values = exported_row['batch_pricing']
                export_retail = money(export_values['含vat价格'])
                if export_values['币种'] != currency or export_retail != retail:
                    raise ValueError('批量导出价格与当前平台快照不一致')
                # 无活动侧且三项营销字段均空时，按含税价成交。百分比活动直接使用导出的 OFF。
                marketing_fields = ('营销价格', '折扣', '活动类型')
                no_marketing = (control.get('company_activity_locked') is False
                                and all(export_values[field] in (None, '') for field in marketing_fields))
                if no_marketing:
                    actual, discount_off = retail, Decimal(0)
                elif export_values['活动类型'] == '直接折扣':
                    actual = money(export_values['营销价格'])
                    discount_off = money(export_values['折扣'])
                    if not 0 <= discount_off < 100:
                        raise ValueError('销售导出的 OFF 百分比无效')
                    if not marketing_price_matches_discount(retail, actual, discount_off, currency):
                        raise ValueError('批量导出营销价与 OFF 折扣不一致')
                else:
                    raise ValueError('销售导出未确认当前活动为百分比折扣或无活动')
                if (raw_actual not in (None, '') or not no_marketing) and money(raw_actual) != actual:
                    raise ValueError('批量导出价格与当前平台快照不一致')
                # 导出库存单位为件，重量为克，ERP 成本为 CNY/件。海外成本与头程仍须按实际仓取明细。
                variant = {**variant, 'overseasStockSum': export_values['海外仓库存'],
                           'marketPrice': actual}
                export_proof = {'ref': exported_row['export_ref'], 'values': export_values}
            else:
                actual = money(raw_actual)
            if not Decimal(0) < actual <= retail:
                raise ValueError('当前营销价格或折扣无效')
            allowed_binding={'configured_live_history_database','live_runtime_database'}
            if capture['synthetic']:allowed_binding.add('injected_synthetic_test')
            if (local_history.get('acquisition_complete') is not True
                    or local_history.get('archive_binding') not in allowed_binding):
                raise ValueError('本程序人工保护与执行档案未取全')
            # 2026-11-20用户确认：overseasStockSum缺失选直发；单位为件，未知保留None，
            # 不伪造零库存。负库存仍阻断；原始来源、采集时间和选模告警随证据保存。
            raw_stock=variant.get('overseasStockSum')
            stock=None if raw_stock in (None,'') else money(raw_stock)
            if stock is not None and stock<0:raise ValueError('海外仓库存为负，无法选择利润公式')
            department=parent.get('department')
            if not isinstance(department,str) or not department.strip():
                raise ValueError('商品部门归属未知，不能选取当前部门费率')
            mode='overseas' if stock is not None and stock>0 else 'direct'
            if export_proof is not None and mode == 'direct':
                variant = {**variant, 'costPrice': export_values['erp成本价'], 'localWeight': export_values['包装重量']}
            rate=select_current_rate(fixed_fees,variant,parent,key.site,mode)
            if export_proof is not None and 'vat_source' in rate:
                category = str(export_values['平台尾类目ID'])
                vat = money(export_values['VAT费率'])
                if category != rate['vat_source']['category_id'] or vat != money(rate['vatRate']):
                    raise ValueError('批量 VAT 税率或尾类目与当前商品不一致')
                rate['vatRate'] = str(vat)
                rate['vat_source'] = {**rate['vat_source'], 'kind': 'sales_sku_batch_export',
                                      'path': '/Api/api-tiktok-sales/online/export', 'field': 'VAT费率'}
            quotes=[row['rate'] for row in sales['currency_rates'] if row.get('name')==currency]
            cny=[row['rate'] for row in sales['currency_rates'] if row.get('name')=='CNY']
            if (sales.get('currency_rate_unit')!='cny_per_currency' or len(quotes)!=1
                    or len(cny)!=1 or money(cny[0])!=1):raise ValueError('公司费用报价单位或基准未确认')
            fee=(compile_overseas_fee(variant,rate,site=key.site,warehouse=key.warehouse,
                company_cny_per_currency=quotes[0]) if mode=='overseas' else
                compile_direct_fee(variant,rate,site=key.site,company_cny_per_currency=quotes[0]))
            if 'vat_source' in rate:
                fee['proof']['vat_source'] = {**rate['vat_source'],
                    'source_ref': export_proof['ref'] if export_proof else control['ref'],
                    'observed_at': sales['completed_at'] if export_proof else control['observed_at']}
            if (type(control.get('company_activity_locked')) is not bool
                    or type(data.get('is_not_for_sale')) is not bool
                    or not isinstance(data.get('product_status'),str) or not isinstance(data.get('status'),str)
                    or not isinstance(sku.get('status_info'),dict) or not isinstance(sku['status_info'].get('status'),str)
                    or type(parent.get('productStatus')) is not int):
                raise ValueError('平台可售或公司活动状态字段未知')
            sellable=(parent['productStatus']==4 and data['product_status']=='ACTIVATE' and data['status']=='ACTIVATE'
                and data['is_not_for_sale'] is False and sku['status_info']['status']=='NORMAL')
            # 官方返回的是日志数量，不含具体生效时间；不能构造last_increase_at。
            try:
                recent = recent_price_log_for(official_history,key)
                if (recent.window_end > observed or recent.window_end+timedelta(seconds=1) <= source_time(capture['queried_at'])
                        or recent.window_end.astimezone(BEIJING).date()!=observed.astimezone(BEIJING).date()):
                    recent = None
            except (ValueError, TypeError, KeyError):
                recent = None  # 历史未知保留事实，交由领域规则标识并告警，不剔除候选。
            pending=any(VariantKey.model_validate(row['key']).target_identity()==key.target_identity()
                and row['state'] not in ('failed','verified') for row in local_history['executions'])
            pending=pending or any(guard['variant_id']==key.target_identity() and guard.get('execution_id') is not None
                for guard in local_history['guards'])

            protected=any(row['shop_id']==key.shop_id and row['site']==key.site and row['main_sku']==target['main_sku']
                for row in local_history['protections'])
            state=ReviewedOnlineState.model_validate({'key':key.model_dump(mode='json'),'main_sku':target['main_sku'],
                'currency':currency,'retail_price':format(retail,'f'),'marketing_price':format(actual,'f'),
                'discount_off_percent':discount_off,
                'sku_price_reference':price_reference_for(price_references,capture,key,
                    sku=target['erp_sku'],currency=currency),
                'fee_scope':'erp-corrected-first-margin-v1',
                'profit_check':{'overseas_stock':None if stock is None else format(stock,'f'),'mode':mode,'department':department,
                    'inventory_observed_at': sales['completed_at'] if export_proof else control['observed_at'],
                    'source_ref': export_proof['ref'] if export_proof else control['ref'],
                    'model':fee['online_model'].model_dump(mode='json')},'observed_at':control['observed_at'],
                'history_known':recent is not None,'last_increase_at':None,
                'recent_price_log':recent.model_dump(mode='json') if recent else None,
                'pending_submission':pending,'protected':protected,'activity_locked':control['company_activity_locked'],
                'sellable':sellable})
            result['online'].append({'ref':control['ref'],'state':state.model_dump(mode='json')})
            result['references']['proofs'][key.identity()]={'fee':fee['proof'],'fixed_fee_source':fixed_fees['source_id'],
                'platform_observed_at':control.get('platform_price_observed_at'),
                'current_components_observed_at':control['observed_at'],'bundle_completed_at':observed.isoformat(),
                'sales_batch_export':export_proof,
                'evaluated_at':evaluated.isoformat()}
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
