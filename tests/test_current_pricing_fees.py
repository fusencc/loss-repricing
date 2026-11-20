"""当前费率回归：外部边界使用合成响应，不调用真实调价。"""
import json
from decimal import Decimal

import httpx
import pytest

from app.config import Settings


@pytest.mark.parametrize('bad_shop', [False, True])
def test_current_capture_replaces_stale_public_and_overseas_rates(tmp_path, bad_shop):
    from app.adapters.current_pricing_fees import capture_current_fees, select_current_rate
    (tmp_path / 'fixed-pricing-fees.json').write_text(json.dumps({
        'schema_version': 'user-fixed-pricing-fees-v2', 'version': 'old',
        'public': {'PH': {'commissionRate': '8.5'}},
        'overseas': {'MY': {'platformRate': '16.74'}},
        'freight': {'PH': {'base_weight_g': '10', 'base_price': '5', 'per_10g_price': '4.5'}}}))
    parents = [{'company_parent': {'bloc': 'GROUP', 'department': 'D1', 'shopName': 'SHOPPH'}}]
    if bad_shop:
        parents.append({'company_parent': {'bloc': 'GROUP', 'department': 'D1', 'shopName': 'BADPH'}})
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'TEST', 'expires_in': 3600})
        if '/supplierrateoversea/' in request.url.path:
            return httpx.Response(200, json={'code': 0, 'count': 1, 'data': [{
                'id': 1, 'blocName': 'GROUP', 'authType': 1, 'siteCode': 'MY',
                'platformRate': '17.28', 'taxVatRate': '0.0001'}]})
        if '/supplierrate/' in request.url.path:
            return httpx.Response(200, json={'resp_code': 0, 'datas': {'code': 0, 'count': 1, 'data': [{
                'id': 2, 'authType': 2, 'blocName': 'GROUP', 'departmentName': 'D1', 'siteCode': 'PH',
                'procedureRate': None, 'commissionRate': None, 'warehouseCost': None,
                'returnRate': '9', 'marketingRate': '8.37', 'otherRate': '4.1',
                'packageFee': '0.3', 'fixedFreight': '0'}]}})
        if request.url.path.endswith('/getPublicRatesByShopName'):
            assert request.url.params['shopName'] in ('SHOPPH', 'BADPH')
            return httpx.Response(200, json={'resp_code': 0, 'datas': [
                {'id': t, 'platform': 'TIKTOK', 'site': 'PH', 'rateType': t,
                 'rateFormat': 1 if t == 4 else 0, 'currency': 'RMB' if t == 4 else '',
                 'associationCategory': False, 'expense': 'bad' if request.url.params['shopName'] == 'BADPH' else v}
                for t, v in [(0, '0'), (1, '2.24'), (2, '7.3'), (4, '1.3')]]})
        pytest.fail(request.url.path)
    settings = Settings(_env_file=None, runtime_dir=tmp_path, vendor_username='test',
                        vendor_password='test', erp_capture_bloc='GROUP', sales_request_interval_seconds=0)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = capture_current_fees(settings, {'capture_id': 'C1', 'synthetic': True},
                                      {'records': parents}, client=client)
    assert source['acquisition_complete'] is (not bad_shop)
    if bad_shop:
        assert source['issues'] == [{'scope': 'BADPH', 'code': 'current_public_unavailable'}]
        with pytest.raises(ValueError):
            select_current_rate(source, {}, {'shopName': 'BADPH'}, 'PH', 'direct')
    rate = select_current_rate(source, {}, parents[0]['company_parent'], 'PH', 'direct')
    assert Decimal(rate['commissionRate']) == Decimal('7.3')
    assert rate['marketingRate'] == '8.37'
    assert select_current_rate(source, {}, {'shopName': 'SHOPMY'}, 'MY', 'overseas')['platformRate'] == '17.28'
    assert source['config']['freight']['PH']['base_price'] == '5'
    assert 'TEST' not in open(source['artifact_file']).read()
    assert len(calls) == (5 if bad_shop else 4)


def test_partial_source_keeps_independent_overseas_rate_without_freight():
    from app.adapters.current_pricing_fees import select_current_rate, SCHEMA
    source = {'schema_version': SCHEMA, 'source_id': 'CURRENT', 'completed_at': '2026-11-19T09:00:00Z',
              'artifact_file': 'synthetic.json', 'acquisition_complete': False,
              'issues': [{'scope': 'BADSHOP', 'code': 'current_public_unavailable'}],
              'config': {'version': 'live', 'overseas': {'PH': {'vatRate': '0.0001'}},
                         'public': {}, 'departments': {}, 'freight': {}}}
    assert select_current_rate(source, {}, {}, 'PH', 'overseas')['vatRate'] == '0.0001'
    with pytest.raises(ValueError):
        select_current_rate(source, {}, {'shopName': 'BADSHOP'}, 'PH', 'direct')


def test_th_public_category_vat_is_not_used_as_a_default():
    from app.adapters.current_pricing_fees import public_values
    rows = [{'platform': 'TIKTOK', 'site': 'TH', 'rateType': t,
             'rateFormat': 1 if t == 4 else 0, 'currency': 'RMB' if t == 4 else '',
             'associationCategory': t == 0, 'expense': '1'} for t in (0, 1, 2, 4)]
    assert public_values(rows, 'TH')['vatRate'] is None


def test_live_selector_rejects_old_fee_archive_and_does_not_fall_back():
    from app.adapters.current_pricing_fees import select_current_rate
    with pytest.raises(ValueError, match='本轮'):
        select_current_rate({'schema_version': 'user-fixed-pricing-fees-v2'}, {}, {}, 'PH', 'direct')


def test_public_rate_missing_or_ambiguous_does_not_reuse_fixed_value():
    from app.adapters.current_pricing_fees import public_values
    with pytest.raises(ValueError):
        public_values([], 'PH')
    rows = [{'platform': 'TIKTOK', 'site': 'PH', 'rateType': t, 'rateFormat': 0,
             'associationCategory': False, 'expense': '1'} for t in (0, 1, 2, 2, 4)]
    with pytest.raises(ValueError):
        public_values(rows, 'PH')
