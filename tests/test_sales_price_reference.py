from decimal import Decimal
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.domain.types import VariantKey


def setup_reference(tmp_path):
    key = dict(platform='TikTok', shop_id='TARGET', site='PH', product_id='P0',
               item_id='I0', sku='PLATFORM-SKU', warehouse='东莞仓库')
    target = dict(erp_shop='TARGETPH', erp_sku='S1', main_sku='M1', key=key)
    capture = dict(capture_id='test-reference', synthetic=True, sample_only=True)
    platform = {'records': [{'erp_shop': 'TARGETPH', 'product_id': 'P0',
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'current_variants': [{'shopName':'TARGETPH', 'productId':'P0',
            'sellerSkuId': 'I0', 'localSellerSku': 'S1',
            'overseasStockSum': '0', 'warehousename': '上海新仓库（东莞）'}]}]}
    settings = Settings(_env_file=None, runtime_dir=tmp_path, sales_request_interval_seconds=0)
    return settings, capture, [target], platform


def peer(i, quantity=1, retail='100', discount='50', **extra):
    return dict(shopName=f'SHOP{i}PH', shopId=f'SHOP{i}', productId=f'P{i}',
        sellerSkuId=f'I{i}', localSellerSku='S1', currency='PHP', sevenDays=quantity,
        priceIncludeVat=retail, discount=discount, overseasStockSum='0',
        warehousename='上海新仓库（东莞）', **extra)


def capture_reference(tmp_path, monkeypatch, rows, *, fail_product=None,
                      core_age_seconds=0, query_clock=None, own_variant=None, older_core=False, query_seconds=31):
    from app.adapters.sales_auth import SalesAuth
    from app.adapters.sales_price_reference import capture_sku_price_references
    monkeypatch.setattr(SalesAuth, 'get_token', lambda self, **kwargs: 'synthetic-token')
    settings, capture, targets, platform = setup_reference(tmp_path)
    platform['records'][0]['observed_at']=(datetime.now(timezone.utc)-timedelta(seconds=core_age_seconds)).isoformat()
    if own_variant:
        platform['records'][0]['current_variants'].append(own_variant)
    if older_core:
        targets.append(dict(targets[0], erp_shop='OLDERPH',
                            key=dict(targets[0]['key'], product_id='OLD', item_id='OLDER')))
        platform['records'].append({'erp_shop':'OLDERPH', 'product_id':'OLD',
            'observed_at':(datetime.now(timezone.utc)-timedelta(seconds=290)).isoformat(),
            'current_variants':[{'sellerSkuId':'OLDER','localSellerSku':'S1'}]})
    requests = []
    def handler(request):
        import json
        body = json.loads(request.content); requests.append((request.url.path, body))
        if query_clock is not None:
            query_clock[0]+=query_seconds
        if request.url.path.endswith('/online/findByPage'):
            parents = [dict(shopName=v['shopName'], productId=v['productId'],
                localMainSku=v.get('_parent_main', 'M1'), productStatus=4, bloc=settings.erp_capture_bloc,
                sevenDaysCount=20-i) for i, v in enumerate(rows)]
            return httpx.Response(200, json=dict(code=0, count=len(parents), data=parents))
        product = body['productId']
        if product == fail_product:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=dict(resp_code=0,
            datas=[v for v in rows if v['productId'] == product]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_sku_price_references(settings, capture, targets, platform=platform, client=client)
    key = VariantKey.model_validate(targets[0]['key']).identity()
    return result, result['records'].get(key), requests


def test_real_capture_reuses_price_fields_and_needs_no_activity_or_fee_calls(tmp_path, monkeypatch):
    result, ref, requests = capture_reference(tmp_path, monkeypatch,
        [peer(1, 8, '100', '50'), peer(2, 7, '80', '25'), peer(3, 6, '100', '0')])
    assert Decimal(ref['marketing_price']) == 70
    assert len(ref['samples']) == 3
    assert all('/online/' in path for path, _ in requests)
    assert result['synthetic'] is True and result['erp_capture_id'] == 'test-reference'


@pytest.mark.parametrize('rows', [[], [peer(1)], [peer(1), peer(2)],
    [peer(1, 0), peer(2, 0), peer(3, 0)]])
def test_missing_top3_or_sales_is_only_missing_reference(tmp_path, monkeypatch, rows):
    result, ref, _ = capture_reference(tmp_path, monkeypatch, rows)
    assert ref is None
    assert 'pricing_ready' not in result and 'status' not in result


def test_one_failed_source_does_not_publish_partial_top3(tmp_path, monkeypatch):
    result, ref, _ = capture_reference(tmp_path, monkeypatch,
        [peer(1), peer(2), peer(3), peer(4)], fail_product='P1')
    assert ref is None
    assert result['issues']  # 只留在可选来源档案，不加入定价issues。


def test_foreign_sku_and_zero_sales_do_not_fill_three_slots(tmp_path, monkeypatch):
    wrong = peer(3); wrong['localSellerSku'] = 'OTHER'
    _, ref, _ = capture_reference(tmp_path, monkeypatch, [peer(1), peer(2, 0), wrong])
    assert ref is None


def test_unknown_discount_is_not_zero_and_explicit_zero_needs_no_more_queries(tmp_path, monkeypatch):
    _, ref, _ = capture_reference(tmp_path, monkeypatch, [peer(1), peer(2), peer(3, discount=None)])
    assert ref is None


def test_confirmed_no_discount_uses_tax_price_with_existing_fields(tmp_path, monkeypatch):
    no_discount=peer(3, discount=None, marketPrice='100')
    _, ref, _=capture_reference(tmp_path, monkeypatch,
        [peer(1, retail='100', discount='0'), peer(2, retail='100', discount='0'), no_discount])
    assert Decimal(ref['marketing_price'])==100


def test_authentication_failure_is_optional_and_never_requests_business_data(tmp_path):
    from app.adapters.sales_price_reference import capture_sku_price_references
    settings, capture, targets, platform=setup_reference(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda request:pytest.fail('未配置账号不请求真实服务'))) as client:
        result=capture_sku_price_references(settings,capture,targets,platform=platform,client=client)
    assert not result['records'] and result['issues']


def test_different_currency_and_warehouse_cannot_supply_top3(tmp_path, monkeypatch):
    wrong_currency=peer(2);wrong_currency['currency']='MYR'
    wrong_warehouse=peer(3);wrong_warehouse['warehousename']='OTHER'
    _, ref, _=capture_reference(tmp_path,monkeypatch,[peer(1),wrong_currency,wrong_warehouse])
    assert ref is None


def test_child_sku_on_a_mixed_parent_is_not_excluded_by_parent_main_sku(tmp_path, monkeypatch):
    mixed=peer(3, 10, '100', '0', _parent_main='OTHER-MAIN')
    _, ref, requests=capture_reference(tmp_path,monkeypatch,[peer(1, 8),peer(2, 7),mixed])
    assert ref is not None
    assert ref['samples'][0]['item_id']=='I3'
    body=requests[0][1]
    assert body['localSellerSkuStr']=='S1'
    assert 'localMainSkuStr' not in body


def test_optional_reference_is_skipped_when_core_snapshot_has_little_freshness_left(tmp_path, monkeypatch):
    _, ref, requests=capture_reference(tmp_path,monkeypatch,[peer(1),peer(2),peer(3)],core_age_seconds=250)
    assert ref is None and requests==[]


def test_slow_optional_queries_stop_on_wall_time_budget_not_after_sixty_requests(tmp_path, monkeypatch):
    from app.adapters import sales_price_reference
    clock=[0]
    monkeypatch.setattr(sales_price_reference,'monotonic',lambda:clock[0])
    _, ref, requests=capture_reference(tmp_path,monkeypatch,[peer(1),peer(2),peer(3)],query_clock=clock)
    assert ref is None and len(requests)==1


def test_another_spec_on_the_target_link_is_not_another_comparable_link(tmp_path, monkeypatch):
    own=peer(9,10);own.update(shopName='TARGETPH',productId='P0',sellerSkuId='ALIAS-I0')
    _, ref, _=capture_reference(tmp_path,monkeypatch,[own,peer(1),peer(2)],own_variant=own)
    assert ref is None



def test_fewer_than_three_other_links_never_queries_variants(tmp_path, monkeypatch):
    own = peer(9)
    own.update(shopName='TARGETPH', productId='P0')
    _, ref, requests = capture_reference(tmp_path, monkeypatch, [own, peer(1), peer(2)])
    assert ref is None
    assert len(requests) == 1 and requests[0][0].endswith('/online/findByPage')


def test_optional_reference_respects_earliest_remaining_core_freshness(tmp_path, monkeypatch):
    from app.adapters import sales_price_reference
    clock = [0]
    monkeypatch.setattr(sales_price_reference,'monotonic',lambda:clock[0])
    _, ref, requests = capture_reference(tmp_path,monkeypatch,[peer(1),peer(2),peer(3)],
                                         query_clock=clock,query_seconds=11,older_core=True)
    assert ref is None and len(requests) == 1
