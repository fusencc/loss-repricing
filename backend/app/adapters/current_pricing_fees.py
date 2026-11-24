"""销售系统是费用与物流配置的唯一来源；北京时间每天首次任务前检查。

配置按哈希归档；同日复用已成功的范围，后续批次只补查失败范围，不回退昨日或Excel。
"""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
from time import sleep
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.normalization import money
from app.adapters.sales_auth import ORIGIN, SalesAuth
from app.adapters.sales_capture import _json_values
from app.adapters.transport_privacy import private_request
from app.services.erp_capture_store import write_verified

SCHEMA = 'sales-daily-pricing-fees-v2'
OVERSEAS = '/Api/api-tiktok-sales/supplierrateoversea/findByPage'
DEPARTMENT = '/Api/api-tiktok-sales/supplierrate/findByPage'
PUBLIC = '/Api/api-b/publicRate/list'
LOGISTICS = '/Api/api-tiktok-sales/suppliership/findByPage'
SHOPS = '/Api/api-tiktok-sales/shop/getShopOptionList'
BEIJING = ZoneInfo('Asia/Shanghai')


def _json(value):
    return json.dumps(_json_values(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _collect(settings, directory, client, *, skip_scopes=()):
    """仅查询尚未成功的范围；店铺仅保留匹配费用所需的属性。"""
    result = {'raw': {}, 'issues': [], 'files': [], 'warnings': []}
    auth = SalesAuth(settings, client)
    context = private_request.set(True)
    specs = [('overseas', OVERSEAS, {'blocNames': [settings.erp_capture_bloc]}),
             ('departments', DEPARTMENT, {'blocName': settings.erp_capture_bloc, 'authType': 2}),
             ('public', PUBLIC, {'platform': 'TIKTOK'}),
             ('logistics', LOGISTICS, {'blocName': settings.erp_capture_bloc}),
             ('shops', SHOPS, {'blocNames': [settings.erp_capture_bloc]})]
    try:
        for name, path, filters in specs:
            if name in skip_scopes:
                continue
            rows, seen, total, page = [], set(), None, 1
            # 店铺属性使用前端原生完整选项列表；分页列表按动态字段排序会跨页重复。
            limit = 1000 if name == 'shops' else 100
            try:
                while True:
                    token = auth.get_token()
                    sleep(settings.sales_request_interval_seconds)
                    body = filters if name == 'shops' else {**filters, 'page': page, 'limit': limit}
                    response = client.post(ORIGIN + path, json=body,
                        headers={'Authorization': 'Bearer ' + token}, timeout=40, follow_redirects=False)
                    response.raise_for_status()
                    if not 200 <= response.status_code < 300:
                        raise ValueError('费用接口重定向未确认')
                    payload = response.json(parse_float=money)
                    if not isinstance(payload, dict): raise ValueError('费用响应必须为对象')
                    if name not in ('overseas', 'public'):
                        if type(payload.get('resp_code')) is not int or payload['resp_code'] != 0:
                            raise ValueError('费用接口业务失败')
                        payload = payload.get('datas')
                        if name == 'shops':
                            if not isinstance(payload, list): raise ValueError('店铺属性完整列表无效')
                            limit = max(1, len(payload))
                            payload = {'code': 0, 'count': len(payload), 'data': payload}
                    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
                        raise ValueError('费用接口业务失败')
                    count, batch = payload.get('count'), payload.get('data')
                    if total is None: total = count
                    if (type(count) is not int or count < 0 or count != total or not isinstance(batch, list)
                            or len(batch) != min(limit, max(0, total - len(rows)))):
                        raise ValueError('费用分页不完整')
                    for row in batch:
                        if not isinstance(row, dict) or row.get('id') is None or str(row['id']) in seen:
                            raise ValueError('费用记录身份缺失或重复')
                        seen.add(str(row['id']))
                    # 原件响应逐页留证，不记录请求头或认证响应。目录仅当前用户可读。
                    file = directory / f'{name}-{page:04d}.json'
                    file.write_bytes(response.content)
                    result['files'].append({'scope': name, 'file': str(file.resolve()), 'path': path, 'request': body,
                        'sha256': sha256(file.read_bytes()).hexdigest()})
                    rows.extend(batch)
                    if len(rows) == total: break
                    page += 1
                # authType 过滤由客户端补做；原始页数和 ID 先核对，避免把过滤后的条数当分页总数。
                unfiltered_count = len(rows)
                if name == 'departments':
                    rows = [r for r in rows if type(r.get('authType')) is int and r['authType'] == 2]
                    skip_code = 'non_department_fee_scope'
                elif name == 'overseas':
                    rows = [r for r in rows if type(r.get('authType')) is int and r['authType'] == 1
                            and not any(r.get(k) for k in ('departmentName','userGroupName','authUserName','shopName'))]
                    skip_code = 'non_group_overseas_fee_scope'
                if len(rows) < unfiltered_count:
                    result['warnings'].append({'scope': name, 'code': skip_code,
                                               'skipped_count': unfiltered_count - len(rows)})
                if not rows:
                    result['raw'][name] = []
                    result['issues'].append({'scope': name, 'code': 'daily_' + name + '_empty'})
                    continue
                if name == 'public':
                    if any(r.get('platform') != 'TIKTOK' for r in rows):
                        raise ValueError('公共费率平台不符')
                elif any(r.get('blocName') != settings.erp_capture_bloc for r in rows):
                    raise ValueError('费用集团归属不符')
                if name == 'shops':
                    rows = [{k: r.get(k) for k in ('id','shopName','blocName','site','mainlandId','shop3pf','property')}
                            for r in rows]
                result['raw'][name] = sorted(rows, key=lambda r: str(r['id']))
            except (httpx.HTTPError, SourceUnavailable, ValueError, KeyError, TypeError, ArithmeticError):
                result['raw'][name] = []
                result['issues'].append({'scope': name, 'code': 'daily_' + name + '_unavailable'})
    finally:
        private_request.reset(context)
    return _json_values(result)


def _read_daily(checked):
    raw = Path(checked['artifact_file']).read_bytes()
    if sha256(raw).hexdigest() != checked['sha256']:
        raise SourceUnavailable('每日费用版本原件校验失败')
    return {**json.loads(raw), **checked}


def refresh_daily_fees(settings, *, client=None, now=None, synthetic=False):
    """持有SQLite事务锁，复用当日完整结果；当日未完成时只补查缺失项。"""
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('销售费用来源地址尚未确认')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('费用检查时间必须带时区')
    day = now.astimezone(BEIJING).date().isoformat()
    scope = sha256(_json([SCHEMA, 'eligible-daily-retry-v1', ORIGIN, SHOPS, PUBLIC,
                          settings.erp_capture_bloc, settings.adapter_mode, bool(synthetic)]).encode()).hexdigest()[:20]
    root = Path(settings.runtime_dir) / 'daily-sales-fees' / scope
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    connection = sqlite3.connect(root / 'checks.sqlite3', timeout=300)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS checks (day TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        connection.commit()
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute('SELECT payload FROM checks WHERE day=?', (day,)).fetchone()
        prior = _read_daily(json.loads(previous[0])) if previous else None
        if prior and prior['acquisition_complete']:
            connection.commit()
            return prior

        # 每次尝试独立留存原件，重试不会覆盖已绑定到其他批次的证据。
        directory = root / 'checks' / day / uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        successful = {name: rows for name, rows in prior['raw'].items() if rows} if prior else {}
        checked_at = now.isoformat()
        owned = client is None
        active = client or httpx.Client(timeout=40, trust_env=False, follow_redirects=False)
        try:
            collected = _collect(settings, directory, active, skip_scopes=successful)
        finally:
            if owned: active.close()
        content = {'raw': {**successful, **collected['raw']}, 'issues': collected['issues']}
        digest = sha256(_json(content).encode()).hexdigest()
        artifact = root / 'versions' / (digest + '.json')
        artifact.parent.mkdir(mode=0o700, exist_ok=True)
        if not artifact.exists():
            write_verified(artifact, {'schema_version': SCHEMA, 'version': 'sales-fees-' + digest, **content})
        checked = {'checked_on': day, 'checked_at': checked_at, 'synthetic': bool(synthetic),
                   'finished_at': datetime.now(timezone.utc).isoformat(),
                   'artifact_file': str(artifact.resolve()), 'sha256': sha256(artifact.read_bytes()).hexdigest(),
                   'acquisition_complete': not collected['issues'],
                   'scope_checked_at': {**({name: prior['scope_checked_at'][name] for name in successful}
                                          if prior else {}),
                                        **dict.fromkeys(collected['raw'], checked_at)},
                   'warnings': [w for w in prior['warnings'] if w['scope'] in successful] + collected['warnings']
                               if prior else collected['warnings'],
                   'files': [f for f in prior['files'] if f['scope'] in successful] + collected['files']
                            if prior else collected['files']}
        write_verified(directory / 'check.json', checked)
        connection.execute('INSERT INTO checks VALUES (?,?) ON CONFLICT(day) DO UPDATE SET payload=excluded.payload',
                           (day, _json(checked)))
        result = _read_daily(checked)
        connection.commit()
        return result
    finally:
        connection.close()


def capture_current_fees(settings, capture, *, daily):
    """只绑定任务启动时的当日检查结果；缺项留给后续批次重试。"""
    captured_day = datetime.fromisoformat(capture['queried_at']).astimezone(BEIJING).date().isoformat()
    if daily['synthetic'] != bool(capture.get('synthetic', False)):
        raise SourceUnavailable('费用检查与本批次采集的运行环境不一致')
    if daily['checked_on'] != captured_day:
        raise SourceUnavailable('费用检查与本批次采集不属于同一北京时间业务日')
    source_id = 'current-fees-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'current-fee-sources' / source_id
    directory.mkdir(mode=0o700, parents=True)
    raw = daily['raw']
    bound_at = datetime.now(timezone.utc).isoformat()
    overseas = {}
    for row in raw['overseas']:
        site = row.get('siteCode')
        if sum(r.get('siteCode') == site for r in raw['overseas']) == 1:
            overseas[site] = {**row, 'vatRate': row.get('taxVatRate')}
    result = {'schema_version': SCHEMA, 'source_id': source_id, 'erp_capture_id': capture['capture_id'],
        'synthetic': bool(capture.get('synthetic', False)), 'sample_only': bool(capture.get('sample_only', False)),
        'queried_at': bound_at, 'completed_at': bound_at,
        'observation_kind': 'local_daily_config_binding',
        'checked_on': daily['checked_on'], 'acquisition_complete': daily['acquisition_complete'],
        'issues': daily['issues'], 'warnings': daily['warnings'], 'daily_source': {k: daily[k] for k in
            ('version','artifact_file','sha256','checked_at','checked_on','scope_checked_at')},
        'config': {'version': daily['version'], 'overseas': overseas, 'departments': raw['departments'],
                   'public': raw['public'], 'freight': raw['logistics'], 'shops': raw['shops']},
        'artifact_file': str((directory / 'result.json').resolve())}
    write_verified(result['artifact_file'], result)
    return result


def _unique(rows, label):
    if len(rows) != 1: raise ValueError(label + '缺少唯一配置，不能回退旧表')
    return rows[0]


def public_values(rows, site):
    """已解析适用范围的公共费用；百分数不除100，仓库成本为CNY/件。"""
    values = {}
    for kind, field in ((0,'vatRate'),(1,'procedureRate'),(2,'commissionRate'),(4,'warehouseCost')):
        if site == 'TH' and kind == 0:
            values[field] = None
            continue
        row = _unique([r for r in rows if r.get('rateType') == kind], '公共费用')
        if (row.get('platform') != 'TIKTOK' or row.get('site') not in (site,'GLOBAL')
                or row.get('associationCategory') is not False
                or row.get('rateFormat') != (1 if kind == 4 else 0)
                or (kind == 4 and row.get('currency') not in ('RMB','CNY'))):
            raise ValueError('当前公共费用范围、单位或类目合同未确认')
        value = money(row.get('expense'))
        if value < 0: raise ValueError('当前公共费用无有效非负值')
        values[field] = str(value)
    return values


def _public_for(rows, shop, site):
    # 公共表与销售店铺的布尔编码相反：公共0=否/1=是；店铺1=否/0=是。
    # 全部(-1/全部)是系统显式通用配置，不是另一个数据源的兜底。
    applicable = []
    for kind in (0,1,2,4):
        if site == 'TH' and kind == 0: continue
        candidates = []
        category_bound = False
        for row in rows:
            if row.get('rateType') != kind: continue
            conditions = [(row.get('site'), site, 'GLOBAL'),
                          (row.get('shopProperty'), shop['property'], '全部'),
                          (row.get('shopMainland'), 1-shop['mainlandId'], -1),
                          (row.get('shop3pf'), 1-shop['shop3pf'], -1)]
            if all(actual in (expected, wildcard) for actual,expected,wildcard in conditions):
                # 类目配置不参与通用费率竞争；仅剩类目配置时才需要该类目合同。
                if row.get('associationCategory') is not False:
                    category_bound |= row.get('associationCategory') is True
                    continue
                candidates.append((tuple(actual==expected for actual,expected,_ in conditions),row))
        if not candidates:
            if category_bound:
                raise ValueError('公共费用只有类目配置，缺少适用类目合同')
            raise ValueError('公共费用缺少唯一配置')
        # 精确站点优先于GLOBAL，其余属性只接受唯一的最具体匹配；不按列表顺序取首条。
        exact_site = [entry for entry in candidates if entry[0][0]]
        candidates = exact_site or candidates
        most_specific = [row for score,row in candidates if not any(
            other != score and all(a >= b for a,b in zip(other,score)) for other,_ in candidates)]
        applicable.append(_unique(most_specific, '公共费用'))
    return public_values(applicable, site)


def select_current_rate(source, variant, parent, site, mode):
    if source.get('schema_version') != SCHEMA:
        raise ValueError('缺少当天销售系统当前费率，不能回退旧表')
    config = source['config']
    if mode == 'overseas':
        try: rate = dict(config['overseas'][site])
        except KeyError: raise ValueError('本轮当前费用未覆盖该站点海外仓') from None
    else:
        shop = _unique([r for r in config.get('shops', []) if r.get('shopName')==parent.get('shopName')], '店铺属性')
        if (shop.get('site') != site or type(shop.get('mainlandId')) is not int or shop['mainlandId'] not in (0,1)
                or type(shop.get('shop3pf')) is not int or shop['shop3pf'] not in (0,1)
                or not shop.get('property') or parent.get('mainlandId') != shop['mainlandId']):
            raise ValueError('当前商品与店铺属性缺失或冲突')
        def scope(row):
            return (row.get('siteCode') == site and row.get('mainlandId') == shop['mainlandId']
                    and row.get('shop3pf') == shop['shop3pf'])
        department = _unique([r for r in config['departments'] if scope(r)
            and r.get('departmentName') == parent.get('department')], '部门费用')
        rate = {**_public_for(config['public'], shop, site), **{k: department.get(k) for k in
            ('returnRate','marketingRate','otherRate','packageFee','fixedFreight')}}
        logistics = _unique([r for r in config['freight'] if scope(r)], '物流模板')
        if logistics.get('priceType') != 1 or money(logistics.get('fixedFreight')) != 0:
            raise ValueError('物流模板须为已核对首续重且固定运费为0')
        rate['freight'] = {name: str(money(logistics.get(key))) for name,key in
            [('base_weight_g','firstWeight'),('base_price','firstAmount'),
             ('step_weight_g','secondWeight'),('step_price','secondAmount')]}
        if money(rate['freight']['base_weight_g']) <= 0 or money(rate['freight']['step_weight_g']) <= 0:
            raise ValueError('物流模板重量单位须为正克数')
        rate['logistics_source'] = {'id': logistics['id'], 'update_time': logistics.get('updateTime')}
        rate['department_source'] = {'id': department.get('id'), 'update_time': department.get('updateTime'),
                                    'mainland_id': shop['mainlandId'], 'shop_3pf': shop['shop3pf']}
        if site == 'TH':
            category = parent.get('tkCategoryIdLeaf')
            if not isinstance(category,str) or not category.strip(): raise ValueError('泰国直发VAT缺少当前商品尾类目')
            try:
                vat = money(parent.get('taxVatRate'))
                if vat < 0: raise ValueError()
            except (ValueError, ArithmeticError):
                raise ValueError('泰国直发VAT缺失或无效，不能使用旧表或填0') from None
            rate['vatRate'] = str(vat)
            rate['vat_source'] = {'contract_version':'sales-parent-category-vat-20261116-v1',
                'kind':'sales_web_parent','path':'/Api/api-tiktok-sales/online/findByPage','field':'taxVatRate',
                'unit':'percent','category_id':category,'shop_name':parent['shopName'],'product_id':parent['productId']}
    return {**rate, 'siteCode': site, 'version': config['version'], 'source_ref': source['source_id'],
        'rate_source': {'contract_version':SCHEMA,'source_id':source['source_id'],
                       'policy':'overseas_fixed' if mode=='overseas' else 'direct_department',
                       'observed_at':source['daily_source']['checked_at'], 'artifact_file':source['artifact_file'],
                       'daily_source':source['daily_source']}}
