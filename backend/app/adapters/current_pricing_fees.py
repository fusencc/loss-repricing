"""11/19陈波确认的当前费用来源：海外仓固定配置；直发公共费用+部门兜底。

仅物流价卡继续来自显式导入的BD表；不再用该表的费率替代当前系统。
抓取失败只影响缺失的站点/部门/店铺，不回退旧值，不改真实商品价格。
"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from time import sleep
from uuid import uuid4

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.fixed_pricing_fees import select_fixed_rate
from app.adapters.normalization import money
from app.adapters.sales_auth import ORIGIN, SalesAuth
from app.adapters.sales_capture import _json_values
from app.adapters.sales_contracts import site_from_shop_name
from app.adapters.transport_privacy import private_request
from app.services.erp_capture_store import write_verified

SCHEMA = 'sales-current-pricing-fees-v1'
OVERSEAS = '/Api/api-tiktok-sales/supplierrateoversea/findByPage'
# TODO(2026-11-20，部门费率来源待排查)：用户确认该页面未及时更新，
# 本接口返回的部门费率不能据此认定为最新费率，已有计算结果需重新核对。
# 用户提供并确认准确的表：D:/钉钉/集团二部2026-11费率配置.xlsx。
# 待用户排查确认后调整取数来源与匹配规则；本次仅备注，尚未切换到该表。
DEPARTMENT = '/Api/api-tiktok-sales/supplierrate/findByPage'
PUBLIC = '/Api/api-tiktok-publish/autoPublishConfig/getPublicRatesByShopName'


def public_values(rows, site):
    """已由店铺接口解析适用范围；百分数不除100，仓库成本为CNY/件。

    rateType=1平台手续费、2基础佣金、4仓库成本；100/101不当作额外佣金。
    TH的类目VAT复用当前父商品，不能用公共默认值替代。
    """
    values = {}
    for kind, field in ((0, 'vatRate'), (1, 'procedureRate'), (2, 'commissionRate'), (4, 'warehouseCost')):
        if site == 'TH' and kind == 0:
            values[field] = None
            continue
        matches = [r for r in rows if r.get('rateType') == kind]
        if len(matches) != 1:
            raise ValueError('当前公共费用缺失或同类费用不唯一')
        row = matches[0]
        if (row.get('platform') != 'TIKTOK' or row.get('site') != site
                or row.get('associationCategory') is not False
                or row.get('rateFormat') != (1 if kind == 4 else 0)
                or (kind == 4 and row.get('currency') != 'RMB')):
            raise ValueError('当前公共费用范围、单位或类目合同未确认')
        value = money(row.get('expense'))
        if value < 0:
            raise ValueError('当前公共费用无有效非负值')
        values[field] = str(value)
    return values


def capture_current_fees(settings, capture, platform, *, client=None):
    if settings.vendor_api_url.rstrip('/') != ORIGIN:
        raise SourceUnavailable('销售费用来源地址尚未确认')
    source_id = 'current-fees-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'current-fee-sources' / source_id
    directory.mkdir(parents=True, mode=0o700)
    result = {'schema_version': SCHEMA, 'source_id': source_id,
              'erp_capture_id': capture['capture_id'], 'synthetic': bool(capture.get('synthetic', False)),
              'sample_only': bool(capture.get('sample_only', False)),
              'queried_at': datetime.now(timezone.utc).isoformat(), 'issues': [], 'files': [],
              'config': {'overseas': {}, 'departments': {}, 'public': {}, 'freight': {}},
              'raw': {'overseas': [], 'departments': {}, 'public': {}},
              'policy': 'current_overseas_fixed_and_direct_department',
              'artifact_file': str((directory / 'result.json').resolve())}
    config = result['config']
    # 仅保留仍在使用的物流价卡。该原件的哈希和版本另存，不冒充实时物流。
    try:
        path = settings.fixed_fee_config_path or Path(settings.runtime_dir) / 'fixed-pricing-fees.json'
        raw = path.read_bytes()
        fixed = json.loads(raw)
        if fixed.get('schema_version') != 'user-fixed-pricing-fees-v2' or not isinstance(fixed['freight'], dict):
            raise ValueError()
        config['freight'] = fixed['freight']
        result['freight_source'] = {'version': fixed['version'], 'sha256': sha256(raw).hexdigest(),
                                    'file': str(path.resolve())}
    except (OSError, ValueError, KeyError, TypeError):
        result['issues'].append({'scope': 'freight', 'code': 'freight_config_unavailable'})

    owned = client is None
    client = client or httpx.Client(timeout=40, trust_env=False, follow_redirects=False)
    auth = SalesAuth(settings, client)
    context = private_request.set(True)
    def read(path, *, body=None, params=None):
        sleep(settings.sales_request_interval_seconds)
        response = client.request('GET' if params is not None else 'POST', ORIGIN + path,
                                  headers={'Authorization': 'Bearer ' + auth.get_token()},
                                  **({'params': params} if params is not None else {'json': body}),
                                  timeout=40, follow_redirects=False)
        response.raise_for_status()
        raw = response.content
        file = directory / f'{len(result["files"])+1:04d}.json'
        file.write_bytes(raw)
        if file.read_bytes() != raw:
            raise OSError('当前费用原件回读不一致')
        result['files'].append({'file': str(file.resolve()), 'path': path,
                                'request': params if params is not None else body,
                                'sha256': sha256(raw).hexdigest(),
                                'observed_at': datetime.now(timezone.utc).isoformat()})
        payload = response.json(parse_float=Decimal)
        code = 'code' if path == OVERSEAS else 'resp_code'
        if not isinstance(payload, dict) or type(payload.get(code)) is not int or payload[code] != 0:
            raise ValueError('当前费用接口业务失败')
        return payload if path == OVERSEAS else payload.get('datas')

    def pages(path, filters):
        rows, seen, total, page = [], set(), None, 1
        while True:
            data = read(path, body={**filters, 'page': page, 'limit': 100})
            if not isinstance(data, dict) or data.get('code') != 0:
                raise ValueError('当前费用分页响应无效')
            batch, count = data.get('data'), data.get('count')
            if total is None:
                total = count
            if (type(count) is not int or count < 0 or count != total or not isinstance(batch, list)
                    or len(batch) != min(100, max(0, count-len(rows)))):
                raise ValueError('当前费用分页不完整')
            for row in batch:
                if not isinstance(row, dict) or row.get('id') is None or str(row['id']) in seen:
                    raise ValueError('当前费用记录身份无效或重复')
                seen.add(str(row['id']))
            rows.extend(batch)
            if len(rows) == total:
                return rows
            page += 1

    parents = [r['company_parent'] for r in (platform or {}).get('records', [])]
    try:
        if parents:
            try:
                rows = pages(OVERSEAS, {'blocNames': [settings.erp_capture_bloc]})
                result['raw']['overseas'] = rows
                if not rows or any(r.get('blocName') != settings.erp_capture_bloc or r.get('authType') != 1
                       or any(r.get(k) for k in ('departmentName', 'userGroupName', 'authUserName', 'shopName')) for r in rows):
                    raise ValueError('海外仓当前固定费率范围冲突')
                for row in rows:
                    site = row['siteCode']
                    if sum(r.get('siteCode') == site for r in rows) != 1:
                        result['issues'].append({'scope': site, 'code': 'current_overseas_ambiguous'})
                        continue
                    config['overseas'][site] = {**row, 'vatRate': row.get('taxVatRate')}
            except (httpx.HTTPError, SourceUnavailable, ValueError, KeyError, TypeError):
                result['issues'].append({'scope': 'overseas', 'code': 'current_overseas_unavailable'})
        departments = sorted({p['department'] for p in parents if p.get('department')})
        for department in departments:
            try:
                rows = pages(DEPARTMENT, {'authType': 2, 'blocName': settings.erp_capture_bloc,
                                           'departmentName': department})
                result['raw']['departments'][department] = rows
                if not rows or any(r.get('authType') != 2 or r.get('blocName') != settings.erp_capture_bloc
                       or r.get('departmentName') != department
                       or any(r.get(k) for k in ('userGroupName', 'authUserName', 'shopName')) for r in rows):
                    raise ValueError('当前部门费率归属冲突')
                for site in {r['siteCode'] for r in rows}:
                    if sum(r.get('siteCode') == site for r in rows) != 1:
                        result['issues'].append({'scope': department + '/' + site, 'code': 'current_department_ambiguous'})
                # 部门原件的公共费率字段为null；只取部门实际负责的五项，
                # 不能用这些null覆盖按店铺解析的公共手续费/佣金/仓库成本。
                config['departments'][department] = {r['siteCode']: {
                    k: r.get(k) for k in ('returnRate', 'marketingRate', 'otherRate', 'packageFee', 'fixedFreight')}
                    for r in rows
                    if sum(v.get('siteCode') == r['siteCode'] for v in rows) == 1}
            except (httpx.HTTPError, SourceUnavailable, ValueError, KeyError, TypeError):
                result['issues'].append({'scope': department, 'code': 'current_department_unavailable'})
        for shop in sorted({p['shopName'] for p in parents}):
            try:
                rows = read(PUBLIC, params={'shopName': shop, 'globalPublish': 'false'})
                if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                    raise ValueError('当前公共费用响应无效')
                result['raw']['public'][shop] = rows
                config['public'][shop] = public_values(rows, site_from_shop_name(shop))
            except (httpx.HTTPError, SourceUnavailable, ValueError, KeyError, TypeError, ArithmeticError):
                result['issues'].append({'scope': shop, 'code': 'current_public_unavailable'})
    finally:
        private_request.reset(context)
        if owned:
            client.close()
    result = _json_values(result)
    digest = sha256(json.dumps(result['config'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result['config']['version'] = 'sales-current-fees-20261119-' + digest[:16]
    result['acquisition_complete'] = not result['issues']
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_verified(result['artifact_file'], result)
    return result


def select_current_rate(source, variant, parent, site, mode):
    if source.get('schema_version') != SCHEMA:
        raise ValueError('缺少本轮销售系统当前费率，不能回退旧表')
    config = source['config']
    try:
        # 复用同一VAT转换函数；公共费用已按店铺取回，禁止跨店套站点默认值。
        public = {} if mode == 'overseas' else {site: config['public'][parent['shopName']]}
        selected = select_fixed_rate({**source, 'acquisition_complete': True,
                                      'config': {**config, 'public': public}}, variant, parent, site, mode)
    except KeyError:
        raise ValueError('本轮当前费用未覆盖该店铺、部门或站点') from None
    selected['rate_source'] = {'contract_version': SCHEMA, 'source_id': source['source_id'],
                               'policy': 'overseas_fixed' if mode == 'overseas' else 'direct_department',
                               'observed_at': source['completed_at'], 'artifact_file': source['artifact_file']}
    return selected
