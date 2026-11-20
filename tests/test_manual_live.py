from datetime import datetime, timezone
from decimal import Decimal
import pytest
from app.adapters.manual_live import validate_authorization, verify_price_evidence
from app.domain.types import VariantKey
from app.config import Settings
from app.db import Base, create_database
from app.services.store import update_settings
from app.services.execution import PricingService


def auth():
    return {'id': 'test-approval', 'business_date': datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat(),
            'key': dict(platform='TikTok', shop_id='shop-1', site='PH', product_id='product-1', item_id='item-1', sku='SKU-1', warehouse='菲律宾海外仓'),
            'shop_name': 'testPH', 'upload_price': '87.18', 'currency': 'PHP',
            'discount_percent_off': '60',
            'actor': 'user', 'approved': True}


def test_authorization_exact_target_and_price():
    a = auth(); key = VariantKey.model_validate(a['key'])
    validate_authorization(a, key, Decimal('87.18'))
    with pytest.raises(ValueError): validate_authorization(a, key, Decimal('87.19'))
    with pytest.raises(ValueError): validate_authorization(a, key.model_copy(update={'item_id': 'other'}), Decimal('87.18'))
    with pytest.raises(ValueError): validate_authorization({**a, 'business_date': '2000-01-01'}, key, Decimal('87.18'))


def evidence():
    a = auth()
    log = dict(id='new-log', type=2, status=2, shopName='testPH', productId='product-1', sellerSkuId='item-1', priceIncludeVat='87.18', priceNew='87.18')
    detail = {'requestBody': {'skus': [{'id': 'item-1', 'price': {'currency': 'PHP', 'amount': '87.18'}}]}, 'responseResult': {'code': 0}}
    current = {'retail': '87.18', 'actual': '34.87', 'currency': 'PHP', 'product_id': 'product-1', 'item_id': 'item-1', 'shop_id': 'shop-1', 'discount': '60'}
    return a, log, detail, current


def test_only_new_successful_bound_receipt_and_current_prices_verify():
    a, log, detail, current = evidence()
    assert verify_price_evidence(a, [], log, detail, current, Decimal('34.872')) == Decimal('34.87')
    for bad in [dict(log, status=1), dict(log, sellerSkuId='other'), dict(log, priceIncludeVat='35')]:
        with pytest.raises(ValueError): verify_price_evidence(a, [], bad, detail, current, Decimal('34.872'))
    with pytest.raises(ValueError): verify_price_evidence(a, ['new-log'], log, detail, current, Decimal('34.872'))
    for bad in [dict(current, actual='14'), dict(current, currency='USD'), dict(current, retail='35'), dict(current, discount='50')]:
        with pytest.raises(ValueError): verify_price_evidence(a, [], log, detail, bad, Decimal('34.872'))
    with pytest.raises(ValueError): verify_price_evidence(a, [], log, dict(detail, responseResult={'code': 99}), current, Decimal('34.872'))


def test_explicit_manual_scope_preserves_automatic_and_other_target_guards(tmp_path):
    engine, factory = create_database(f'sqlite:///{tmp_path}/live.db')
    Base.metadata.create_all(engine)
    key = VariantKey.model_validate(auth()['key'])
    update_settings(factory, {'pilot_shops': ['shop-1'], 'site_enabled': ['PH']})
    class Scoped:
        mode = 'live'
        def readiness(self):
            return {'contract_ready': True, 'verified_sites': ['PH'], 'manual_targets': [key.target_identity()]}
    cfg = Settings(_env_file=None, live_submit_enabled=True, coordination_required=True, live_writer_exclusive=False)
    service = PricingService(factory, Scoped(), cfg)
    assert service._allowed(key, manual=True)[0]
    assert not service._allowed(key, manual=False)[0]
    assert not service._allowed(key.model_copy(update={'item_id': 'other'}), manual=True)[0]
    engine.dispose()


def test_upload_timeout_preserves_intent_and_never_posts_twice(tmp_path):
    from types import SimpleNamespace
    from app.adapters.manual_live import ManualLiveAdapter
    from app.domain.types import Decision
    from openpyxl import load_workbook
    a = auth(); key = VariantKey.model_validate(a['key'])
    adapter = ManualLiveAdapter(Settings(_env_file=None, live_submit_enabled=True), a, tmp_path)
    adapter.snapshot = SimpleNamespace(online=[SimpleNamespace(observed_at=datetime.now(timezone.utc))])
    adapter.previous_ids = ['old-log']
    calls = []
    def timeout(*args, **kwargs):
        calls.append(args)
        raise TimeoutError()
    adapter._request = timeout
    decision = Decision(key=key, status='待处理', ready=True, reason_code='manual_correction', reason='authorized',
        source_row_ids=[], upload_price=Decimal('87.18'), upload_column='税后价格', trace={'correction_id': 'test'})
    assert adapter.submit('execution-one', decision).status == 'unknown'
    assert adapter.submit('execution-one', decision).status == 'unknown'
    assert len(calls) == 1
    assert (tmp_path/'intent-execution-one.json').exists()
    sheet = load_workbook(tmp_path/'upload-execution-one.xlsx').active
    assert sheet['B2'].value == 'product-1' and sheet['C2'].value == 'item-1'
    assert Decimal(str(sheet['E2'].value)) == Decimal('87.18')
    assert all(sheet.cell(2, n).value is None for n in (4,6,7,8,9))
    assert adapter.submit('other', decision.model_copy(update={'upload_price': Decimal('87.19')})).status == 'rejected'
    assert len(calls) == 1


def test_observed_success_receipt_with_trailing_comma_is_accepted_only():
    from app.adapters.http import HttpAdapter
    result = HttpAdapter._upload_result({'resp_code': 0, 'resp_msg': '上传完成,', 'datas': None}, {})
    assert result.status == 'accepted'
    assert result.task_id is None


def test_one_cent_native_conversion_requires_matching_net_price_and_logistics():
    a, log, detail, current = evidence()
    log['priceNew'] = '82.19'
    detail['requestBody']['skus'][0]['price']['amount'] = '82.19'
    current.update(retail='87.19', actual='34.88', tax_exclusive='82.19', logistics='5.00', vat_percent='0')
    assert verify_price_evidence(a, [], log, detail, current, Decimal('34.872')) == Decimal('34.88')
    with pytest.raises(ValueError):
        verify_price_evidence(a, [], log, detail, dict(current, tax_exclusive='82.18'), Decimal('34.872'))
    with pytest.raises(ValueError):
        verify_price_evidence(a, [], log, detail, dict(current, retail='87.20', logistics='5.01'), Decimal('34.872'))


@pytest.mark.parametrize('current_retail,expected', [('35', 'rejected'), ('87.18', 'unknown')])
def test_explicit_row_rejection_requires_unchanged_price_and_no_new_task(tmp_path, monkeypatch, current_retail, expected):
    import json
    import app.adapters.manual_live as module
    a = {**auth(), 'baseline_retail': '35', 'baseline_actual': '14'}
    key = VariantKey.model_validate(a['key'])
    adapter = module.ManualLiveAdapter(Settings(_env_file=None), a, tmp_path)
    (tmp_path/'intent-one.json').write_text(json.dumps({'authorization':a, 'previous_log_ids':[]}))
    (tmp_path/'upload-response-one.json').write_text(json.dumps({'resp_code':0,'resp_msg':'上传完成,第1行店铺名不存在;'}))
    adapter._logs = lambda: []
    raw = {'acquisition_complete':True,'records':[{
        'current_variants':[{'sellerSkuId':'item-1','shopId':'shop-1','currency':'PHP','priceIncludeVat':current_retail,'marketPrice':'14'}],
        'data':{'skus':[{'id':'item-1','price':{'currency':'PHP','sale_price':current_retail}}]}}]}
    monkeypatch.setattr(module, 'capture_platform_controls', lambda *args,**kwargs: raw)
    assert adapter.lookup('one',None,key).status == expected
    adapter._logs = lambda: [{'id':'new','type':2}]
    assert adapter.lookup('one',None,key).status == 'unknown'


@pytest.mark.parametrize('site,currency',[('MY','MYR'),('TH','THB')])
def test_exact_manual_target_authorization_supports_current_test_sites(site,currency):
    a=auth();a['key']['site']=site;a['currency']=currency
    validate_authorization(a,VariantKey.model_validate(a['key']),Decimal('87.18'))
    with pytest.raises(ValueError):validate_authorization({**a,'currency':'PHP'},VariantKey.model_validate(a['key']),Decimal('87.18'))


def test_product_percentage_promotion_is_verified_against_current_variant(tmp_path):
    from app.adapters.manual_live import ManualLiveAdapter
    a=auth();a['key']['site']='MY';a['currency']='MYR'
    ad=ManualLiveAdapter(Settings(_env_file=None),a,tmp_path)
    parent={'shopid':'shop-1','promotionId':'promo','promotionType':2,'productType':1,'status':2}
    product={'shopid':'shop-1','productId':'product-1','promotionId':'promo','sellerSkuId':None,'promotionPrice':None,'discount':'60','priceIncludeVat':None,'marketPrice':None}
    calls=[]
    def request(method,path,**kwargs):
        calls.append(kwargs['json'])
        return {'code':0,'count':1,'data':[parent if path.endswith('findByPage') else product]}
    ad._request=request
    v={'promotionId':'promo','discount':'60','marketPrice':'4','priceIncludeVat':'10'}
    assert ad._promotion(v)['kind']=='percentage_discount'
    assert calls[-1]['productType']==1 and 'sellerSkuIds' not in calls[-1]
    product['promotionPrice']='4'
    with pytest.raises(ValueError):ad._promotion(v)
    product['promotionPrice']=None;product['productId']='other'
    with pytest.raises(ValueError):ad._promotion(v)


def test_my_one_cent_readback_requires_exact_native_vat_and_logistics():
    a,log,detail,current=evidence()
    a['key']['site']='MY';a['currency']='MYR';a['upload_price']='6.45'
    log.update(priceIncludeVat='6.45',priceNew='5.81')
    detail['requestBody']['skus'][0]['price']={'currency':'MYR','amount':'5.81'}
    current.update(retail='6.46',actual='2.584',currency='MYR',tax_exclusive='5.81',logistics='0.06',vat_percent='10')
    assert verify_price_evidence(a,[],log,detail,current,Decimal('2.58'))==Decimal('2.584')
    with pytest.raises(ValueError):verify_price_evidence(a,[],log,detail,dict(current,vat_percent='9'),Decimal('2.58'))
    with pytest.raises(ValueError):verify_price_evidence(a,[],log,detail,dict(current,tax_exclusive='5.80'),Decimal('2.58'))


def test_th_native_vat_price_uses_observed_upward_cent_rounding():
    a,log,detail,current=evidence()
    a['key']['site']='TH';a['currency']='THB';a['upload_price']='76.53'
    log.update(priceIncludeVat='76.53',priceNew='61.26')
    detail['requestBody']['skus'][0]['price']={'currency':'THB','amount':'61.26'}
    current.update(retail='76.54',actual='30.616',currency='THB',tax_exclusive='61.26',logistics='0.40',vat_percent='24.12')
    assert verify_price_evidence(a,[],log,detail,current,Decimal('30.612'))==Decimal('30.616')
