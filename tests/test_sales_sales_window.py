"""真实主SKU归属及混挂链接销量回归。"""
import json
from collections import Counter

import httpx
import pytest
from app.config import Settings


def parent(pid, main='MAIN', quantity=24):
    return dict(id=pid, shopName='SHOPPH', shopId='S1', localMainSku=main,
                productId=pid, sevenDaysCount=quantity)


def variant(pid, item, sku, quantity):
    return dict(shopName='SHOPPH', shopId='S1', productId=pid, sellerSkuId=item,
                localSellerSku=sku, localMainSku=None, sevenDays=quantity)


def run_capture(tmp_path, monkeypatch, *, parents=None, variants=None, mapping=None, targets=None, fault=None):
    from app.adapters.sales_sales_window import capture_sales_window
    monkeypatch.setattr('app.adapters.sales_auth.SalesAuth.get_token', lambda self: 'synthetic')
    parents = parents if parents is not None else [parent('P1'), parent('P2', quantity=0), parent('P3', 'OTHER', 7)]
    variants = variants if variants is not None else {
        'P1': [variant('P1', 'I1', 'X1', 19), variant('P1', 'I2', 'B1', 5)],
        'P2': [variant('P2', 'I3', 'X1', 0)],
        'P3': [variant('P3', 'I4', 'X1', 1), variant('P3', 'I5', 'B1', 6)]}
    mapping = mapping if mapping is not None else {'X1': 'MAIN', 'B1': 'OTHER'}
    targets = targets if targets is not None else [dict(erp_shop='SHOPPH', shop_id='S1', main_sku='MAIN', erp_sku='X1')]
    calls = []
    def respond(request):
        body = json.loads(request.content); path = request.url.path; calls.append((path, body))
        if fault:
            response = fault(path, body)
            if response is not None: return response
        if path.endswith('/login'):
            return httpx.Response(200, json={'success': True, 'code': 200, 'data': {'token': 'erp-test'}})
        if path.endswith('/queryGoodsSales'):
            rows = [{'sku': s, 'mainSku': mapping[s]} for s in body['skuList'] if s in mapping]
            return httpx.Response(200, json={'success': True, 'code': 200, 'data': rows, 'total': len(rows), 'hasNext': False})
        if '/findProductVariants/' in path:
            return httpx.Response(200, json={'resp_code': 0, 'datas': variants[path.rsplit('/', 1)[1]]})
        assert path.endswith('/findByPage') and body['shopNameStr'] == 'SHOPPH'
        if 'localSellerSkuStr' in body:
            skus = set(body['localSellerSkuStr'].split())
            rows = [p for p in parents if any(v['localSellerSku'] in skus for v in variants[p['productId']])]
        else:
            assert body['mainSkuType'] == 1
            rows = [p for p in parents if p['localMainSku'] in body['localMainSkuStr'].split()]
        offset = (body['page'] - 1) * body['limit']
        return httpx.Response(200, json={'code': 0, 'count': len(rows), 'data': rows[offset:offset+body['limit']]})
    settings = Settings(_env_file=None, runtime_dir=tmp_path, sales_request_interval_seconds=0,
                        erp_username='test', erp_password='test', erp_token='')
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = capture_sales_window(settings, {'capture_id': 'C1', 'synthetic': True, 'sample_only': False},
                                      targets, client=client, page_size=1)
    return result, calls


def test_mixed_link_excludes_foreign_sales_and_finds_own_sku_under_other_parent(tmp_path, monkeypatch):
    result, calls = run_capture(tmp_path, monkeypatch)
    row = result['volumes'][0]
    assert row['quantity'] == 20  # 19 + 0 + 1, not parent 24 or 31.
    assert row['discovered_scope_complete'] and result['acquisition_complete']
    assert not row['complete']
    assert row['product_ids'] == ['P1', 'P2', 'P3']
    assert result['basis'] == 'sales_system_web_sku_seven_days'
    assert result['window_start'] is None and result['window_end'] is None
    assert sorted(Counter(p for p, _ in calls if '/findProductVariants/' in p).values()) == [1, 1, 1]
    assert {x['true_main_sku'] for x in result['contributions']} == {'MAIN', 'OTHER'}


def both_targets():
    return [dict(erp_shop='SHOPPH', shop_id='S1', main_sku=m, erp_sku=s) for m, s in [('MAIN', 'X1'), ('OTHER', 'B1')]]


def test_two_target_mains_share_link_without_double_counting(tmp_path, monkeypatch):
    result, _ = run_capture(tmp_path, monkeypatch, targets=both_targets())
    assert {r['main_sku']: r['quantity'] for r in result['volumes']} == {'MAIN': 20, 'OTHER': 11}


@pytest.mark.parametrize('bad', [None, '', True, -1, 1.5])
def test_invalid_own_quantity_remains_unknown(tmp_path, monkeypatch, bad):
    result, _ = run_capture(tmp_path, monkeypatch, parents=[parent('P1')], variants={'P1': [variant('P1', 'I1', 'X1', bad)]})
    assert result['volumes'][0]['quantity'] is None and not result['volumes'][0]['complete']


def test_bad_foreign_quantity_does_not_block_known_zero(tmp_path, monkeypatch):
    result, _ = run_capture(tmp_path, monkeypatch, parents=[parent('P1')], variants={
        'P1': [variant('P1', 'I1', 'X1', 0), variant('P1', 'I2', 'B1', None)]})
    assert result['volumes'][0]['quantity'] == 0 and result['volumes'][0]['discovered_scope_complete']


def test_missing_mapping_cannot_guess_from_parent_or_prefix(tmp_path, monkeypatch):
    result, _ = run_capture(tmp_path, monkeypatch, mapping={'X1': 'MAIN'})
    assert result['volumes'][0]['quantity'] is None and not result['acquisition_complete']


def test_duplicate_variant_is_unknown(tmp_path, monkeypatch):
    v = variant('P1', 'I1', 'X1', 19)
    result, _ = run_capture(tmp_path, monkeypatch, parents=[parent('P1')], variants={'P1': [v, v]})
    assert result['volumes'][0]['quantity'] is None


def test_duplicate_parent_page_is_unknown(tmp_path, monkeypatch):
    def fault(path, body):
        if path.endswith('findByPage') and body['page'] == 2:
            return httpx.Response(200, json={'code': 0, 'count': 2, 'data': [parent('P1')]})
    result, _ = run_capture(tmp_path, monkeypatch, fault=fault)
    assert result['volumes'][0]['quantity'] is None


def test_rate_limit_stops_sales_calls_without_zero_fallback(tmp_path, monkeypatch):
    def fault(path, body):
        if path.endswith('findByPage'): return httpx.Response(429, json={})
    result, calls = run_capture(tmp_path, monkeypatch, fault=fault)
    assert len(calls) == 1 and result['volumes'][0]['quantity'] is None


def test_bad_quantity_only_blocks_its_real_main(tmp_path, monkeypatch):
    result, _ = run_capture(tmp_path, monkeypatch, parents=[parent('P1')], targets=both_targets(), variants={
        'P1': [variant('P1', 'I1', 'X1', 2), variant('P1', 'I2', 'B1', None)]})
    rows = {r['main_sku']: r for r in result['volumes']}
    assert rows['MAIN']['quantity'] == 2 and rows['MAIN']['discovered_scope_complete']
    assert rows['OTHER']['quantity'] is None and not rows['OTHER']['complete']


def test_child_reverse_query_must_include_already_observed_links(tmp_path, monkeypatch):
    def fault(path, body):
        if path.endswith('findByPage') and 'localSellerSkuStr' in body:
            return httpx.Response(200, json={'code': 0, 'count': 0, 'data': []})
    result, _ = run_capture(tmp_path, monkeypatch, fault=fault)
    assert result['volumes'][0]['quantity'] is None
    assert not result['acquisition_complete']


@pytest.mark.parametrize('rows', [[], [dict(variant('P1', 'I1', 'X1', 2), shopId='OTHER')]])
def test_missing_or_foreign_shop_variants_are_unknown(tmp_path, monkeypatch, rows):
    result, _ = run_capture(tmp_path, monkeypatch, parents=[parent('P1')], variants={'P1': rows})
    assert result['volumes'][0]['quantity'] is None


def test_parent_total_is_not_used_as_quantity(tmp_path, monkeypatch):
    result, _ = run_capture(tmp_path, monkeypatch, parents=[parent('P1', quantity=None)],
                            variants={'P1': [variant('P1', 'I1', 'X1', 2)]})
    assert result['volumes'][0]['quantity'] == 2


def test_disconnected_true_main_sibling_cannot_make_partial_sales_complete(tmp_path, monkeypatch):
    result, _ = run_capture(tmp_path, monkeypatch,
        parents=[parent('P1', 'MAIN', 2), parent('P2', 'OTHER', 30)],
        variants={'P1': [variant('P1', 'I1', 'X1', 2)],
                  'P2': [variant('P2', 'I2', 'X2', 30)]},
        mapping={'X1': 'MAIN', 'X2': 'MAIN'})
    row = result['volumes'][0]
    assert row['quantity'] == 2  # Reference only; the actual total could be 32.
    assert not row['complete']
    assert row['discovered_scope_complete']
