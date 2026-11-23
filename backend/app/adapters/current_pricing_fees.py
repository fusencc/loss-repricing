"""销售系统是费用与物流配置的唯一来源；北京时间每天首次任务前查询一次。

配置内容按哈希归档，检查日期单独持久化；同日跨批次、重启及并发复用。
失败范围不回退昨日或Excel；批次只绑定当天检查结果，不重新查询费用。
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


def _collect(settings, directory, client):
    """五类配置统一读取；店铺仅投影匹配费用所需属性，避免无关在线统计导致换版。"""
    result = {'raw': {}, 'issues': [], 'files': []}
    auth = SalesAuth(settings, client)
    context = private_request.set(True)
    specs = [('overseas', OVERSEAS, {'blocNames': [settings.erp_capture_bloc]}),
             ('departments', DEPARTMENT, {'blocName': settings.erp_capture_bloc, 'authType': 2}),
             ('public', PUBLIC, {'platform': 'TIKTOK'}),
             ('logistics', LOGISTICS, {'blocName': settings.erp_capture_bloc}),
             ('shops', SHOPS, {'blocNames': [settings.erp_capture_bloc]})]
    try:
        for name, path, filters in specs:
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
                        if name == 'public':
                            if row.get('platform') != 'TIKTOK': raise ValueError('公共费率平台不符')
                        elif row.get('blocName') != settings.erp_capture_bloc:
                            raise ValueError('费用集团归属不符')
                        if name == 'departments' and row.get('authType') != 2:
                            raise ValueError('仅使用部门费率，不使用个人或店铺覆盖')
                        if name == 'overseas' and (row.get('authType') != 1 or
                                any(row.get(k) for k in ('departmentName','userGroupName','authUserName','shopName'))):
                            raise ValueError('仅使用集团海外仓费率')
                    # 原件响应逐页留证，不记录请求头或认证响应。目录仅当前用户可读。
                    file = directory / f'{name}-{page:04d}.json'
                    file.write_bytes(response.content)
                    result['files'].append({'file': str(file.resolve()), 'path': path, 'request': body,
                        'sha256': sha256(file.read_bytes()).hexdigest()})
                    rows.extend(batch)
                    if len(rows) == total: break
                    page += 1
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


def refresh_daily_fees(settings, *, client=None, now=None, synthetic=False):
    """每天首次检查持有SQLite事务锁，其他进程等待并复用同一结果。

    每日检查与内容版本分开。完整或部分失败的当日结果均记录，不以旧数据顶替。
    翻日后的任务重查；版本文件只在内容改变时创建，不重写未变化的配置。
    """
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('销售费用来源地址尚未确认')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('费用检查时间必须带时区')
    day = now.astimezone(BEIJING).date().isoformat()
    scope = sha256(_json([SCHEMA, ORIGIN, SHOPS, PUBLIC, settings.erp_capture_bloc, settings.adapter_mode, bool(synthetic)]).encode()).hexdigest()[:20]
    root = Path(settings.runtime_dir) / 'daily-sales-fees' / scope
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    connection = sqlite3.connect(root / 'checks.sqlite3', timeout=300)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS checks (day TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        connection.commit()
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute('SELECT payload FROM checks WHERE day=?', (day,)).fetchone()
        if previous:
            checked = json.loads(previous[0])
        else:
            directory = root / 'checks' / day
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            owned = client is None
            active = client or httpx.Client(timeout=40, trust_env=False, follow_redirects=False)
            try:
                collected = _collect(settings, directory, active)
            finally:
                if owned: active.close()
            content = _json({'raw': collected['raw'], 'issues': collected['issues']})
            digest = sha256(content.encode()).hexdigest()
            artifact = root / 'versions' / (digest + '.json')
            artifact.parent.mkdir(mode=0o700, exist_ok=True)
            if not artifact.exists():
                write_verified(artifact, {'schema_version': SCHEMA, 'version': 'sales-fees-' + digest,
                                         **json.loads(content)})
            checked = {'checked_on': day, 'checked_at': now.isoformat(),
                       'finished_at': datetime.now(timezone.utc).isoformat(),
                       'artifact_file': str(artifact.resolve()), 'sha256': sha256(artifact.read_bytes()).hexdigest(),
                       'files': collected['files']}
            write_verified(directory / 'check.json', checked)
            connection.execute('INSERT INTO checks VALUES (?,?)', (day, _json(checked)))
        raw = Path(checked['artifact_file']).read_bytes()
        if sha256(raw).hexdigest() != checked['sha256']:
            raise SourceUnavailable('每日费用版本原件校验失败')
        result = {**json.loads(raw), **checked}
        result['acquisition_complete'] = not result['issues']
        connection.commit()
        return result
    finally:
        connection.close()


def capture_current_fees(settings, capture, platform, *, client=None):
    """给本批次绑定当天费用版本；不按商品、店铺或批次重复取数。"""
    daily = refresh_daily_fees(settings, client=client, synthetic=bool(capture.get('synthetic', False)))
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
        'issues': daily['issues'], 'daily_source': {k: daily[k] for k in
            ('version','artifact_file','sha256','checked_at','checked_on')},
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
        for row in rows:
            if row.get('rateType') != kind: continue
            conditions = [(row.get('site'), site, 'GLOBAL'),
                          (row.get('shopProperty'), shop['property'], '全部'),
                          (row.get('shopMainland'), 1-shop['mainlandId'], -1),
                          (row.get('shop3pf'), 1-shop['shop3pf'], -1)]
            if all(actual in (expected, wildcard) for actual,expected,wildcard in conditions):
                if row.get('associationCategory') is not False:
                    raise ValueError('公共费用涉及类目关联，缺少适用类目合同，不能替换为普通费率')
                candidates.append((tuple(actual==expected for actual,expected,_ in conditions),row))
        if not candidates: raise ValueError('公共费用缺少唯一配置')
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
