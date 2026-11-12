"""按明确子SKU读取ERP主SKU目录，仅用身份字段，不使用无日期的销量数字。

合同来自当前ERP开放接口GoodsSalesParam/GoodsSalesResult：skuList、分页、
sku/mainSku；2026-11-11真实67个SKU全部返回。原件保留，登录正文不归档。
"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4

import httpx

from app.adapters.base import SourceUnavailable
from app.adapters.erp_rates import ALLOWED_BASES
from app.adapters.normalization import sku_text
from app.adapters.transport_privacy import private_request
from app.services.erp_capture_store import write_verified

PATH = '/goods/queryGoodsSales'


def capture_sku_catalog(settings, skus, *, erp_capture_id, synthetic, sample_only,
                        client=None, page_size=1000, batch_size=1000, progress=None):
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError('主SKU目录页大小须为1至1000')
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError('主SKU目录批大小须为1至1000')
    requested = sorted({sku_text(sku) for sku in skus})
    base = settings.erp_open_api_url.rstrip('/')
    if base not in ALLOWED_BASES:
        raise SourceUnavailable('ERP主SKU目录服务地址未确认')
    source_id = 'erp-sku-catalog-' + uuid4().hex
    directory = Path(settings.runtime_dir) / 'erp-sku-catalogs' / source_id
    directory.mkdir(mode=0o700, parents=True)
    result = {'schema_version':'erp-sku-parent-catalog-v1','source_id':source_id,
        'erp_capture_id':erp_capture_id,'synthetic':synthetic,'sample_only':sample_only,
        'queried_at':datetime.now(timezone.utc).isoformat(),'requested_skus':requested,
        'records':[],'mapping':{},'files':[],'issues':[],'pricing_ready':False,
        'artifact_file':str((directory / 'result.json').resolve())}
    owned = client is None
    client = client or httpx.Client(timeout=60, trust_env=False, follow_redirects=False)
    context = private_request.set(True)

    def payload(response):
        response.raise_for_status()
        value = response.json(parse_float=Decimal)
        if (not isinstance(value, dict) or type(value.get('code')) is not int
                or value['code'] != 200 or value.get('success') is not True):
            raise ValueError('catalog_business_failed')
        return value

    try:
        token = settings.erp_token
        if requested and not token:
            if not settings.erp_username or not settings.erp_password.get_secret_value():
                raise SourceUnavailable('ERP主SKU目录认证未配置')
            try:
                user = payload(client.post(base + '/system/innerOpen/login', json={
                    'username':settings.erp_username,'password':settings.erp_password.get_secret_value()},
                    timeout=60,follow_redirects=False)).get('data')
                token = user.get('accessToken') or user.get('token') if isinstance(user, dict) else None
            except (httpx.HTTPError, ValueError):
                raise SourceUnavailable('ERP主SKU目录认证失败') from None
        if requested and (not isinstance(token, str) or not token.strip()):
            raise SourceUnavailable('ERP主SKU目录未返回有效认证')
        token = re.sub(r'^Bearer\s+', '', token or '', flags=re.IGNORECASE).strip()
        for offset in range(0, len(requested), batch_size):
            batch = requested[offset:offset+batch_size]
            number = offset // batch_size + 1
            observed, seen, page, total = [], set(), 1, None
            try:
                while True:
                    if progress:
                        progress(f'读取ERP主SKU目录：第{number}批，第{page}页。')
                    body = {'skuList':batch,'pageNum':page,'pageSize':page_size}
                    response = client.post(base + PATH, json=body,
                        headers={'Authorization':'Bearer ' + token},timeout=60,follow_redirects=False)
                    path = directory / f'{len(result["files"])+1:05d}.json'
                    path.write_bytes(response.content)
                    raw = path.read_bytes()
                    if raw != response.content:
                        raise OSError('主SKU目录原件回读不一致')
                    result['files'].append({'file':str(path.resolve()),'bytes':len(raw),
                        'sha256':sha256(raw).hexdigest(),'path':PATH,'request':body,
                        'http_status':response.status_code,'received_at':datetime.now(timezone.utc).isoformat()})
                    value = payload(response)
                    rows, count = value.get('data'), value.get('total')
                    if not isinstance(rows, list) or type(count) is not int or count < 0:
                        raise ValueError('catalog_pagination_incomplete')
                    if total is None:
                        total = count
                    if (count != total or total > len(batch)
                            or len(rows) != min(page_size, max(0, total-len(observed)))
                            or type(value.get('hasNext')) is not bool
                            or value['hasNext'] != (len(observed)+len(rows) < total)):
                        raise ValueError('catalog_pagination_incomplete')
                    for index, row in enumerate(rows):
                        if not isinstance(row, dict):
                            raise ValueError('catalog_scope_mismatch')
                        sku, main = sku_text(row.get('sku')), sku_text(row.get('mainSku'))
                        if sku not in batch or sku in seen:
                            raise ValueError('catalog_scope_mismatch')
                        seen.add(sku)
                        observed.append({'sku':sku,'main_sku':main,
                            'ref':f'{source_id}/batch/{number}/page/{page}/row/{index}'})
                    if len(observed) == total:
                        result['records'].extend(observed)
                        result['mapping'].update({row['sku']:row['main_sku'] for row in observed})
                        break
                    page += 1
            except (httpx.HTTPError, ValueError) as error:
                code = str(error) if type(error) is ValueError and str(error).startswith('catalog_') else 'catalog_read_failed'
                result['issues'].append({'code':code,'batch':number,'skus':batch})
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
                    break
    except SourceUnavailable:
        result['issues'].append({'code':'catalog_authentication_failed'})
    finally:
        private_request.reset(context)
        if owned:
            client.close()
    result['missing_skus'] = sorted(set(requested) - result['mapping'].keys())
    result['acquisition_complete'] = not result['issues'] and not result['missing_skus']
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_verified(result['artifact_file'], result)
    return result
