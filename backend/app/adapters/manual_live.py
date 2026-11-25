"""明确授权的单商品真实提价；不声明自动四候选采集合同已完成。

2026-11-17用户指定含税价并要求真实提交。这是人工修正适配器，复用现有
修正服务、数据库意图和目标锁。合同依据官方Excel模板、销售前端及已存原件。
百分比活动先核对活动明细，生效需新任务的平台成功回执和变化后的当前价格。
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import re
from pathlib import Path
from uuid import uuid4
import httpx

from app.adapters.base import LookupResult, SubmitResult
from app.adapters.excel import XLSX_MIME, price_upload_workbook
from app.adapters.http import HttpAdapter, UPLOAD_PATH
from app.adapters.normalization import money
from app.adapters.sales_auth import ORIGIN, SalesAuth
from app.adapters.sales_capture import capture_sales_sources, _json_values
from app.adapters.sales_platform_capture import capture_platform_controls
from app.adapters.current_pricing_fees import capture_current_fees, refresh_daily_fees
from app.adapters.sales_price_reference import capture_sku_price_references
from app.adapters.live_online_preparation import marketing_price_matches_discount, prepare_online_states
from app.adapters.erp_rates import query_current_erp_fx
from app.adapters.transport_privacy import private_request
from app.domain.money import MoneyRatio
from app.domain.price_reference import saved_price_limit
from app.domain.engine import SITE_CURRENCIES
from app.domain.types import OnlineState, SiteContract, Snapshot, VariantKey, FeeModel
from app.services.local_price_history import capture_local_price_history
from app.adapters.official_price_logs import capture_recent_price_changes
from app.services.erp_capture_store import write_verified
from app.services.store import BEIJING, utcnow


def validate_authorization(authorization, key, upload_price):
    a = authorization
    if (a.get('approved') is not True or not a.get('id') or not a.get('actor')
            or a.get('business_date') != utcnow().astimezone(BEIJING).date().isoformat()
            or VariantKey.model_validate(a['key']) != key or key.site not in ('PH','MY','TH')
            or a.get('currency') != SITE_CURRENCIES[key.site] or not a.get('shop_name')
            or money(a['upload_price']) != upload_price or upload_price <= 0
            or upload_price != upload_price.quantize(Decimal('.01'))):
        raise ValueError('不符合当日单商品、币种及精确金额授权')


def verify_price_evidence(authorization, previous_ids, log, detail, current, expected_actual):
    a, k = authorization, authorization['key']
    if (str(log['id']) in previous_ids or log.get('type') != 2 or log.get('status') != 2
            or log.get('shopName') != a['shop_name'] or log.get('productId') != k['product_id']
            or log.get('sellerSkuId') != k['item_id'] or money(log['priceIncludeVat']) != money(a['upload_price'])):
        raise ValueError('缺少本次新增且同目标同含税价的成功任务')
    body, reply = detail['requestBody'], detail['responseResult']
    if isinstance(body, str): body = json.loads(body)
    if isinstance(reply, str): reply = json.loads(reply)
    skus = [s for s in body['skus'] if s.get('id') == k['item_id']]
    if (reply.get('code') != 0 or len(skus) != 1 or skus[0]['price'].get('currency') != a['currency']
            or money(skus[0]['price']['amount']) != money(log['priceNew'])):
        raise ValueError('平台回执没有确认本SKU及任务税前价格')
    retail, uploaded = money(current['retail']), money(a['upload_price'])
    retail_matches = retail == uploaded
    if not retail_matches and abs(retail-uploaded) <= Decimal('.01'):
        # PH、MY及2026-11-19 TH实测：税前请求/回读相同，含税价由税前+物流乘当前VAT向上取分。
        # 只解释有完整构成证据的一分差；营销价0.02容限仍独立校验。
        net, logistics, vat = (money(current[n]) for n in ('tax_exclusive','logistics','vat_percent'))
        native_retail = ((MoneyRatio(net)+MoneyRatio(logistics))
            * (MoneyRatio(Decimal(1))+MoneyRatio(vat)/MoneyRatio(Decimal(100))))
        retail_matches = (net == money(log['priceNew']) and logistics >= 0 and vat >= 0
            and retail == native_retail.round_to_step(Decimal('.01'),'ROUND_UP'))
    if (any(current.get(n) != k[n] for n in ('shop_id', 'product_id', 'item_id'))
            or current['currency'] != a['currency'] or not retail_matches
            or money(current['discount']) != money(a['discount_percent_off'])
            or (expected_actual is not None and abs(money(current['actual']) - expected_actual) > Decimal('.02'))):
        raise ValueError('当前含税零售价、营销价、折扣或币种尚未与提交一致')
    return money(current['actual'])


def effective_marketing_fields(variant, retail, activity):
    # 已确认无营销活动且无有效折扣时，沿用在线算价入口的含税成交口径。
    actual, discount = variant.get('marketPrice'), variant.get('discount')
    if (activity is False and (actual in (None, '') or money(actual) == retail)
            and (discount in (None, '') or money(discount) == 0)):
        return retail, Decimal(0)
    return money(actual), money(discount)


class ManualLiveAdapter:
    mode = 'live'

    def __init__(self, settings, authorization, directory, client=None, *, auth=None):
        self.settings, self.authorization = settings, authorization
        self.key = VariantKey.model_validate(authorization['key'])
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(timeout=60, trust_env=False, follow_redirects=False)
        self.auth = auth or SalesAuth(settings, self.client)
        self.snapshot = None

    def readiness(self):
        try:
            validate_authorization(self.authorization, self.key, money(self.authorization['upload_price']))
            valid = self.auth.configured and self.settings.vendor_api_url.rstrip('/') == ORIGIN
        except (ValueError, KeyError): valid = False
        return {'contract_ready': valid, 'verified_sites': [self.key.site] if valid else [],
                'manual_targets': [self.key.target_identity()] if valid else [],
                'contract_scope': 'explicit_manual_single_sku', 'automatic_collection_ready': False}

    def _request(self, method, path, **kwargs):
        context = private_request.set(True)
        try:
            response = self.client.request(method, ORIGIN + path,
                headers={'Authorization': 'Bearer ' + self.auth.get_token()}, **kwargs)
            response.raise_for_status()
            value = _json_values(response.json(parse_float=Decimal))
            # 只归档业务返回；请求头及凭据从不落盘。平台详情可能含敏感嵌套字段。
            def redact(v):
                if isinstance(v, dict):
                    return {k: '[REDACTED]' if any(t in k.lower() for t in ('token','secret','password','authorization')) else redact(x) for k,x in v.items()}
                if isinstance(v, list): return [redact(x) for x in v]
                if isinstance(v, str) and v.startswith(('{','[')):
                    try: return redact(json.loads(v, parse_float=Decimal))
                    except ValueError: return v
                if isinstance(v, str) and re.search(r'access.?token|authorization|client.?secret|password', v, re.I):
                    return '[REDACTED_AUTH_TEXT]'
                return v
            write_verified(self.directory / ('response-' + uuid4().hex + '.json'),
                _json_values({'path': path, 'observed_at': utcnow().isoformat(), 'http_status': response.status_code, 'response': redact(value)}))
            return value
        finally: private_request.reset(context)

    def _logs(self):
        a = self.authorization
        rows, total = [], None
        for page in range(1, 101):
            value = self._request('POST', '/Api/api-tiktok-sales/operationLog/findByPage', json={
                'page': page, 'limit': 100, 'shopNames': a['shop_name'], 'productIds': self.key.product_id,
                'createTimeStart': a['business_date'] + ' 00:00:00', 'createTimeEnd': a['business_date'] + ' 23:59:59'})
            if value.get('resp_code') != 0: raise ValueError('操作日志读取失败')
            data = value['datas']; count = data['count']
            if type(count) is not int or count < 0 or (total is not None and count != total):
                raise ValueError('操作日志总数变化，需重新读取')
            total = count; rows.extend(data['data'])
            if len(rows) == total: return rows
            if not data['data'] or len(rows) > total: break
        raise ValueError('操作日志分页未完整')

    def _promotion(self, variant):
        a, k = self.authorization, self.key
        promotion = variant['promotionId']
        value = self._request('POST', '/Api/api-tiktok-sales/promotion/findByPage', json={
            'page': 1, 'limit': 100, 'shopNameList': [a['shop_name']], 'promotionIds': [promotion], 'productIds': [k.product_id]})
        if value.get('code') != 0 or value.get('count') != 1 or len(value['data']) != 1:
            raise ValueError('活动身份不唯一')
        p = value['data'][0]
        if (p['shopid'] != k.shop_id or p['promotionId'] != promotion or p['promotionType'] != 2
                or p['productType'] not in (1,2) or p['status'] != 2): raise ValueError('不是已核对的进行中百分比折扣活动')
        body = {'page':1, 'limit':100, 'promotionId':promotion, 'productType':p['productType'], 'productIds':[k.product_id]}
        if p['productType'] == 2: body['sellerSkuIds'] = [k.item_id]
        value = self._request('POST', '/Api/api-tiktok-sales/promotion/productList', json=body)
        if value.get('code') != 0 or value.get('count') != 1 or len(value['data']) != 1:
            raise ValueError('活动子商品不唯一')
        row = value['data'][0]
        discount = money(a['discount_percent_off'])
        if (row['productId'] != k.product_id or row['shopid'] != k.shop_id
                or row['promotionPrice'] is not None or money(row['discount']) != discount
                or money(variant['discount']) != discount):
            raise ValueError('活动归属、固定价或折扣与当前变体不一致')
        # 产品级百分比活动只返回父商品及折扣；SKU级另核对具体商品与价格。
        if p['productType'] == 2 and (row['sellerSkuId'] != k.item_id
                or money(row['priceIncludeVat']) != money(variant['priceIncludeVat'])
                or money(row['marketPrice']) != money(variant['marketPrice'])):
            raise ValueError('活动子商品或价格与当前变体不一致')
        if not 0 <= discount < 100 or not marketing_price_matches_discount(
                money(variant['priceIncludeVat']), money(variant['marketPrice']), discount, a['currency']):
            raise ValueError('当前营销价不符合已授权折扣比例')
        return {'kind': 'percentage_discount', 'discount_percent_off': str(discount), 'promotion_id': promotion, 'fixed_promotion_price': None}

    def collect(self, business_date):
        a, k = self.authorization, self.key
        validate_authorization(a, k, money(a['upload_price']))
        if business_date.isoformat() != a['business_date']: raise ValueError('单次授权已跨日')
        daily_fees = refresh_daily_fees(self.settings, client=self.client)
        capture = {'capture_id': 'manual-' + uuid4().hex, 'synthetic': False, 'sample_only': True, 'queried_at': utcnow().isoformat(),
            'groups': [{'rows': [{'orderid': 'explicit-manual-price', 'fields': {'item ID': k.product_id}}]}]}
        target = {'key': k.model_dump(mode='json'), 'erp_shop': a['shop_name'], 'main_sku': a['main_sku'], 'erp_sku': k.sku}
        sales = capture_sales_sources(self.settings, capture, client=self.client)
        platform = capture_platform_controls(self.settings, capture, [{'erp_shop': a['shop_name'], 'product_id': k.product_id, 'item_id': k.item_id}], client=self.client)
        official_history = capture_recent_price_changes(self.settings,[k],client=self.client)
        from app.adapters.official_price_logs import recent_price_log_for
        if recent_price_log_for(official_history,k).count != 0:
            raise ValueError('近72小时已有调价日志，不调价')
        history = capture_local_price_history(self.settings, capture)
        fixed = capture_current_fees(self.settings, capture, daily=daily_fees)
        fx = query_current_erp_fx(self.settings)
        for name, source in [('sales',sales),('platform',platform),('history',history),('fixed',fixed),('fx',fx)]:
            write_verified(self.directory / (capture['capture_id']+'-'+name+'.json'), source)
        if not platform.get('acquisition_complete') or len(platform['records']) != 1: raise ValueError('当前来源未完整')
        control = platform['records'][0]
        variants = [v for v in control['current_variants'] if v['sellerSkuId'] == k.item_id]
        if len(variants) != 1: raise ValueError('子商品不唯一')
        target['erp_sku'] = variants[0]['localSellerSku']
        activity = self._promotion(variants[0])
        # 原件不修改；“参加活动”不同于“固定活动价锁住”。本入口只接已核对百分比活动。
        prepared_platform = deepcopy(platform)
        prepared_platform['records'][0]['company_activity_locked'] = False
        price_references = capture_sku_price_references(self.settings,capture,[target],
            platform=prepared_platform,client=self.client)
        prepared = prepare_online_states(capture, [target], sales=sales, platform=prepared_platform,
            local_history=history, fixed_fees=fixed, official_history=official_history,
            price_references=price_references)
        write_verified(self.directory / (capture['capture_id']+'-preparation.json'), {'preparation': prepared, 'activity': activity, 'authorization': a})
        if prepared['issues'] or len(prepared['online']) != 1: raise ValueError(str(prepared['issues']))
        state = dict(prepared['online'][0]['state'], fee_scope='normal')
        online = OnlineState.model_validate(state)
        if (online.retail_price != money(a['baseline_retail']) or online.marketing_price != money(a['baseline_actual'])):
            raise ValueError('当前价或折扣已改变，原授权基线失效')
        contracts = {r['contract']['site']: SiteContract.model_validate(r['contract']) for r in prepared['contracts']}
        self.snapshot = Snapshot(as_of=utcnow(), business_date=business_date, fx_date=fx['date'], fx_rates=fx['source_rates'],
            fx_rate_unit='cny_per_currency', lines=[], online=[online], contracts=contracts, complete_sources=[],
            source_versions={'manual_contract': 'explicit-manual-ph-my-th-20261119-v2', 'fixed_fees': fixed['config']['version'],
                             'authorization': a['id']})
        self.previous_ids = [str(r['id']) for r in self._logs()]
        write_verified(self.directory / 'latest-snapshot.json', self.snapshot.model_dump(mode='json'))
        return self.snapshot

    def submit(self, execution_id, decision):
        try:
            if not decision.trace.get('correction_id'):
                raise ValueError('缺少人工修正授权')
            if self.snapshot is None or (utcnow()-self.snapshot.online[0].observed_at).total_seconds() > 300:
                raise ValueError('提交前实时数据缺失或过期')
        except (ValueError, KeyError):
            return SubmitResult('rejected', message='单商品授权或提交前数据未通过')
        return self.upload_confirmed_price(execution_id, decision, self.previous_ids)

    def upload_confirmed_price(self, execution_id, decision, previous_log_ids):
        # 单商品授权与已获授权的名单批次共用正式上传。调用方先持久化执行、核对现价并记录原日志。
        try:
            validate_authorization(self.authorization, decision.key, decision.upload_price)
            if (not self.settings.live_submit_enabled or not decision.ready or decision.upload_column != '税后价格'):
                raise ValueError('真实开关或单商品上传字段无效')
            intent = {'execution_id': execution_id, 'authorization': self.authorization, 'decision': decision.model_dump(mode='json'),
                'previous_log_ids': previous_log_ids, 'created_at': utcnow().isoformat()}
            intent_path = self.directory / ('intent-'+execution_id+'.json')
            if intent_path.exists(): return SubmitResult('unknown', message='已有上传意图，仅核验，不重传')
            write_verified(intent_path, intent)
            book = price_upload_workbook(decision, self.authorization['shop_name'])
            (self.directory / ('upload-'+execution_id+'.xlsx')).write_bytes(book.getvalue())
        except (ValueError, KeyError): return SubmitResult('rejected', message='单商品授权或提交前数据未通过')
        try:
            with book:
                value = self._request('POST', UPLOAD_PATH, files={'file': ('price.xlsx', book, XLSX_MIME)})
        except Exception: return SubmitResult('unknown', message='上传结果未知；持久意图保留，只查询不重传')
        write_verified(self.directory / ('upload-response-'+execution_id+'.json'), value)
        return HttpAdapter._upload_result(value, {'single_data_row_number': 2})

    def lookup(self, execution_id, task_id, key, *, check_amount=True):
        try:
            intent = json.loads((self.directory / ('intent-'+execution_id+'.json')).read_text())
            if key != self.key or intent['authorization'] != self.authorization:
                raise ValueError('核验身份冲突')
            a, k = self.authorization, self.key
            rows = self._logs()
            matches = [r for r in rows if str(r['id']) not in intent['previous_log_ids'] and r.get('type') == 2
                and r.get('shopName') == a['shop_name'] and r.get('productId') == k.product_id
                and r.get('sellerSkuId') == k.item_id and r.get('priceIncludeVat') is not None
                and money(r['priceIncludeVat']) == money(a['upload_price'])]
            if len(matches) != 1:
                # 本次实测只有一行数据，明确返回第1行店名不存在。结合无新任务及
                # 当前价仍为原基线，才能把旧unknown终结为失败；不直接重发旧意图。
                reply_path = self.directory / ('upload-response-'+execution_id+'.json')
                reply = json.loads(reply_path.read_text()) if reply_path.exists() else {}
                rejected = (reply.get('resp_code') == 0 and reply.get('resp_msg') == '上传完成,第1行店铺名不存在;')
                new_rows = [r for r in rows if str(r['id']) not in intent['previous_log_ids']]
                if rejected and not new_rows:
                    readback = capture_platform_controls(self.settings,
                        {'capture_id':'reject-'+execution_id,'synthetic':False,'sample_only':True},
                        [{'erp_shop':a['shop_name'],'product_id':k.product_id,'item_id':k.item_id}], client=self.client)
                    write_verified(self.directory / ('rejected-readback-'+execution_id+'.json'), readback)
                    if readback.get('acquisition_complete') and len(readback['records']) == 1:
                        control = readback['records'][0]
                        values = [v for v in control['current_variants'] if v['sellerSkuId'] == k.item_id]
                        skus = [s for s in control['data']['skus'] if s['id'] == k.item_id]
                        if (len(values) == len(skus) == 1 and values[0]['shopId'] == k.shop_id
                                and values[0]['currency'] == skus[0]['price']['currency'] == a['currency']
                                and money(values[0]['priceIncludeVat']) == money(skus[0]['price']['sale_price']) == money(a['baseline_retail'])
                                and money(values[0]['marketPrice']) == money(a['baseline_actual'])):
                            return LookupResult('rejected', message='本次唯一数据行被拒绝：店铺名不存在；无新任务且平台/销售价格未变')
                raise ValueError('本次目标价任务尚未唯一出现')
            log = matches[0]
            write_verified(self.directory / 'latest-task.json', log)
            if log['status'] != 2:
                return LookupResult('unknown', message='调价任务状态：'+str(log['status'])+'；继续核验，不重传')
            detail = self._request('GET', '/Api/api-tiktok-sales/operationLog/getRequestAndResponseById', params={'id': log['id']})
            if detail.get('resp_code') != 0: raise ValueError('调价任务详情未取得')
            capture = {'capture_id': 'verify-'+execution_id, 'synthetic': False, 'sample_only': True}
            platform = capture_platform_controls(self.settings, capture, [{'erp_shop': a['shop_name'], 'product_id': k.product_id, 'item_id': k.item_id}], client=self.client)
            write_verified(self.directory / ('readback-'+uuid4().hex+'.json'), platform)
            if not platform['acquisition_complete'] or len(platform['records']) != 1: raise ValueError('回读未完整')
            control = platform['records'][0]
            variants = [v for v in control['current_variants'] if v['sellerSkuId'] == k.item_id]
            skus = [s for s in control['data']['skus'] if s['id'] == k.item_id]
            if len(variants) != 1 or len(skus) != 1: raise ValueError('回读SKU不唯一')
            v, s = variants[0], skus[0]
            if (s['price']['currency'] != a['currency'] or money(s['price']['sale_price']) != money(v['priceIncludeVat'])
                    or s['seller_sku'] != k.sku or v['sellerSku'] != k.sku): raise ValueError('平台与公司当前价不同步')
            retail = money(v['priceIncludeVat'])
            actual, discount = effective_marketing_fields(v, retail, control.get('company_activity_locked'))
            current = {'retail': str(retail), 'actual': str(actual), 'discount': str(discount), 'currency': v['currency'],
                       'tax_exclusive': s['price']['tax_exclusive_price'], 'logistics': v['logisticsCost'],
                       'vat_percent': control['company_parent']['taxVatRate'],
                       'product_id': v['productId'], 'item_id': v['sellerSkuId'], 'shop_id': v['shopId']}
            if v.get('marketPrice') in (None, ''):
                current['marketing_source_field'] = 'company.priceIncludeVat (confirmed_no_activity)'
            expected = money(intent['decision']['trace']['rounded_actual']) if check_amount else None
            actual = verify_price_evidence(a, intent['previous_log_ids'], log, detail['datas'], current, expected)
            full = FeeModel.model_validate(intent['decision']['trace']['online_rows'][0]['profit_check']['model'])
            upper = saved_price_limit(intent['decision']['trace'])
            actual_check = full.evaluate(MoneyRatio(actual))
            if check_amount and upper is None and not actual_check.meets_minimum_margin:
                raise ValueError('回读营销价在线利润率未达到1%')
            observed = utcnow()
            # 官方统计/最新成功价只作独立证据补充，不代替已绑定任务和当前平台价。
            # 按原提交北京时间日期查count；latest只有金额，保留其无任务ID/时间的限制。
            from app.adapters.official_price_logs import capture_official_price_logs, build_price_record
            submitted_at = intent['created_at']
            official = capture_official_price_logs(self.settings, k, a['shop_name'],
                datetime.fromisoformat(submitted_at).astimezone(BEIJING).date(), self.directory, client=self.client)
            price_record = build_price_record(execution_id, k, a['shop_name'], money(a['upload_price']),
                log, current, observed, official, submitted_at=submitted_at)
            write_verified(self.directory / ('price-record-'+execution_id+'-'+uuid4().hex+'.json'), price_record)
            archive_prefix = 'verified-' if check_amount else 'observed-'
            write_verified(self.directory / (archive_prefix+execution_id+'.json'), {'log': log, 'current': current,
                'actual_profit': str(actual_check.profit.decimal()),
                'minimum_margin_required': upper is None,
                'online_margin_passed': actual_check.meets_minimum_margin,
                'observed_at': observed.isoformat(),
                'freshness_basis': '提交前价格及日志基线+本次新增任务平台成功回执+提交后平台及公司价格一致',
                'platform_update_time': control['data'].get('update_time')})
            return LookupResult('accepted', actual, observed, None, 'actual_including_vat',
                '平台任务'+str(log['id'])+'成功；上传含税'+a['upload_price']+'，回读含税'+str(current['retail'])+
                '，营销价'+str(actual)+'；税前及物流换算已核对，在线利润已记录', price_record=price_record)
        except Exception as exc:
            return LookupResult('unknown', message='当前生效证据尚未齐备（'+type(exc).__name__+'），仅核验')
