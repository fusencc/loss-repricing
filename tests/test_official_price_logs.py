from datetime import datetime, timezone
from decimal import Decimal
import json
import httpx
import pytest
from app.config import Settings
from app.domain.types import VariantKey

KEY = VariantKey(platform='TikTok',shop_id='shop-1',site='PH',product_id='product-1',item_id='item-1',sku='SKU-1',warehouse='菲律宾海外仓')
NOW = datetime(2026,11,17,2,tzinfo=timezone.utc)


def test_official_requests_and_exact_decimal_archive(tmp_path, monkeypatch):
    from app.adapters.official_price_logs import capture_official_price_logs
    monkeypatch.setattr('app.adapters.official_price_logs.SalesAuth.get_token', lambda self:'test-token')
    calls=[]
    def handle(req):
        calls.append(req)
        assert req.method=='POST' and req.headers['authorization']=='Bearer test-token'
        body=json.loads(req.content)
        if req.url.path.endswith('getLatestSuccessPrice'):
            assert body=={'shopName':'testPH','productId':'product-1','sellerSkuId':'item-1'}
            return httpx.Response(200,content=b'{"resp_code":0,"datas":82.190000000000000001}')
        assert body=={'sellerSkuIds':['item-1'],'type':2,'completeTimeStart':'2026-11-17 00:00:00','completeTimeEnd':'2026-11-17 23:59:59'}
        return httpx.Response(200,json={'resp_code':0,'datas':[{'sellerSkuId':'item-1','count':1}]})
    result=capture_official_price_logs(Settings(_env_file=None),KEY,'testPH',NOW.date(),tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert len(calls)==2
    assert result['count_query']['count']==1
    assert result['latest_success_query']['amount']=='82.190000000000000001'
    assert result['latest_success_query']['price_basis']=='unconfirmed'
    assert 'test-token' not in ''.join(p.read_text() for p in tmp_path.glob('*.json'))
    assert json.loads((tmp_path/result['archive_file']).read_text())==result


@pytest.mark.parametrize('payload,status,count',[
    ({'resp_code':0,'datas':[{'sellerSkuId':'item-1','count':0}]},'ok',0),
    ({'resp_code':0,'datas':[]},'invalid_response',None),
    ({'resp_code':0,'datas':[{'sellerSkuId':'other','count':1}]},'invalid_response',None),
    ({'resp_code':0,'datas':[{'sellerSkuId':'item-1','count':True}]},'invalid_response',None),
    ({'resp_code':1,'resp_msg':'查询失败'},'business_error',None),
])
def test_missing_or_failed_count_is_not_zero(tmp_path,monkeypatch,payload,status,count):
    from app.adapters.official_price_logs import capture_official_price_logs
    monkeypatch.setattr('app.adapters.official_price_logs.SalesAuth.get_token',lambda self:'token')
    def handle(req):
        if req.url.path.endswith('getLatestSuccessPrice'):raise httpx.ReadTimeout('secret',request=req)
        return httpx.Response(200,json=payload)
    r=capture_official_price_logs(Settings(_env_file=None),KEY,'testPH',NOW.date(),tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert r['count_query']['status']==status and r['count_query']['count']==count
    assert r['latest_success_query']['status']=='transport_error'
    assert r['latest_success_query']['amount'] is None
    assert 'secret' not in json.dumps(r)


def inputs():
    log={'id':'log-1','shopName':'testPH','productId':'product-1','sellerSkuId':'item-1',
         'type':2,'status':2,'priceNew':'82.19','priceIncludeVat':'87.18','operator':'operator',
         'createTime':'2026-11-17 09:48:17','completeTime':'2026-11-17 09:48:48'}
    current={'shop_id':'shop-1','product_id':'product-1','item_id':'item-1','currency':'PHP',
        'retail':'87.19','tax_exclusive':'82.19','actual':'34.88','logistics':'5.00','discount':'60'}
    official={'key':KEY.model_dump(mode='json'),'shop_name':'testPH','queried_at':NOW.isoformat(),
        'count_query':{'status':'ok','count':1},'latest_success_query':{'status':'ok','amount':'82.19'}}
    return log,current,official


def test_four_prices_stay_separate_and_official_value_is_only_compared():
    from app.adapters.official_price_logs import build_price_record
    log,current,official=inputs()
    r=build_price_record('execution-1',KEY,'testPH',Decimal('87.18'),log,current,NOW,official)
    assert {k:v['amount'] for k,v in r['prices'].items()}=={
        'uploaded_tax_inclusive':'87.18','tax_exclusive':'82.19','actual_tax_inclusive':'87.19','marketing':'34.88'}
    assert r['official_comparison']=='matches_log_tax_exclusive'
    assert r['differences']['actual_tax_inclusive_minus_uploaded']=='0.01'
    assert all(v['currency']=='PHP' and v['source_field'] and v['observed_at'] for v in r['prices'].values())
    official['latest_success_query']['amount']='90'
    r=build_price_record('execution-1',KEY,'testPH',Decimal('87.18'),log,current,NOW,official)
    assert r['official_comparison']=='different_from_log_tax_exclusive'
    assert r['prices']['tax_exclusive']['amount']=='82.19'
    log['sellerSkuId']='other'
    with pytest.raises(ValueError):build_price_record('execution-1',KEY,'testPH',Decimal('87.18'),log,current,NOW,official)


@pytest.fixture
def execution_db(tmp_path):
    from app.db import Base,create_database
    from app.models import Batch,Item,Execution,VariantGuard
    from app.domain.types import Decision
    from app.services.store import stamp
    engine,factory=create_database(f'sqlite:///{tmp_path}/live.db')
    Base.metadata.create_all(engine)
    decision=Decision(key=KEY,status='待核验',ready=True,reason_code='manual_correction',reason='test',source_row_ids=[],
        target_actual=Decimal('34.872'),upload_price=Decimal('87.18'),upload_column='税后价格',
        trace={'online_rows':[{'profit_check':{'model':{
            'revenue_factor':'0.64','fixed_cost':'20','fixed_cost_divisor':'1','version':'test','verified':True}}}]})
    with factory.begin() as s:
        s.add(Batch(id='batch',run_key='manual:test',mode='live',business_date='2026-11-17',created_at=stamp(),status='completed',snapshot={},summary={}))
        s.flush()
        s.add(Item(id='item',batch_id='batch',variant_id=KEY.identity(),key=KEY.model_dump(mode='json'),status='已核验',phase='verified',reason='test',decision=decision.model_dump(mode='json'),updated_at=stamp()))
        s.flush()
        s.add(Execution(id='execution-1',item_id='item',variant_id=KEY.target_identity(),state='verified',
            request=decision.model_dump(mode='json'),response={},evidence={'mode':'live','actual_price':'34.88',
            'confirmed_at':'2026-11-17T01:53:08+00:00','effective_at':'2026-11-17T01:53:08+00:00'},created_at=stamp(),updated_at=stamp()))
        s.add(VariantGuard(variant_id=KEY.target_identity(),execution_id=None,last_effective_at='2026-11-17T01:53:08+00:00'))
    yield factory
    engine.dispose()


def record_result():
    from app.adapters.official_price_logs import build_price_record
    log,current,official=inputs()
    return build_price_record('execution-1',KEY,'testPH',Decimal('87.18'),log,current,NOW,official)


def test_read_only_supplement_persists_four_prices_without_resetting_history(execution_db,tmp_path):
    from app.services.price_log_archive import archive_verified_price_record
    from app.adapters.base import LookupResult
    from app.models import Execution,VariantGuard,Audit
    record=record_result()
    class ReadOnly:
        mode='live'
        calls=0
        def lookup(self,*args):
            self.calls+=1
            return LookupResult('accepted',Decimal('34.88'),datetime.now(timezone.utc),price_record=record)
        def submit(self,*args):raise AssertionError('must not upload')
    adapter=ReadOnly()
    with execution_db() as s:
        before=s.get(Execution,'execution-1').evidence.copy()
    result=archive_verified_price_record(execution_db,adapter,'execution-1',tmp_path/'archive')
    assert adapter.calls==1 and result['execution_state']=='verified'
    with execution_db() as s:
        e=s.get(Execution,'execution-1');guard=s.get(VariantGuard,KEY.target_identity())
        assert e.evidence['price_record']==record
        assert all(e.evidence[k]==v for k,v in before.items())
        assert guard.execution_id is None and guard.last_effective_at==before['effective_at']
        assert s.query(Audit).count()==1
    assert (tmp_path/'archive'/result['archive_directory']/'price-record.json').exists()


def test_successful_reconcile_saves_structured_price_record(execution_db,tmp_path):
    from app.services.execution import PricingService
    from app.adapters.base import LookupResult
    from app.models import Execution,VariantGuard
    from app.services.store import utcnow
    record=record_result()
    with execution_db.begin() as s:
        e=s.get(Execution,'execution-1');e.state='unknown';e.evidence={}
        s.get(VariantGuard,KEY.target_identity()).execution_id=e.id
    class Lookup:
        mode='live'
        def lookup(self,*args):return LookupResult('accepted',Decimal('34.88'),utcnow(),price_record=record)
    PricingService(execution_db,Lookup(),Settings(_env_file=None,runtime_dir=tmp_path,coordination_required=False)).reconcile()
    with execution_db() as s:
        e=s.get(Execution,'execution-1')
        assert e.state=='verified' and e.evidence['price_record']==record


def test_manual_lookup_collects_official_logs_and_returns_four_prices(tmp_path,monkeypatch):
    import app.adapters.manual_live as module
    log,current,official=inputs()
    a={'key':KEY.model_dump(mode='json'),'shop_name':'testPH','currency':'PHP','upload_price':'87.18','discount_percent_off':'60'}
    intent={'authorization':a,'previous_log_ids':[],'created_at':'2026-11-17T01:48:17+00:00',
        'decision':{'trace':{'rounded_actual':'34.872','online_rows':[{'profit_check':{'model':{
            'revenue_factor':'0.64','fixed_cost':'20','fixed_cost_divisor':'1','version':'test','verified':True}}}]}}}
    (tmp_path/'intent-execution-1.json').write_text(json.dumps(intent))
    adapter=module.ManualLiveAdapter(Settings(_env_file=None),a,tmp_path)
    adapter._logs=lambda:[log]
    adapter._request=lambda *args,**kwargs:{'resp_code':0,'datas':{'requestBody':{'skus':[{'id':'item-1','price':{'amount':'82.19','currency':'PHP'}}]},'responseResult':{'code':0}}}
    # 税前价加物流为 87.19；零 VAT 证明回读含税价比上传价高一分的来源。
    raw={'acquisition_complete':True,'records':[{'company_parent':{'taxVatRate':'0'},
        'data':{'update_time':1,'skus':[{'id':'item-1','seller_sku':'SKU-1',
        'price':{'currency':'PHP','sale_price':'87.19','tax_exclusive_price':'82.19'}}]},'current_variants':[{
            'sellerSkuId':'item-1','sellerSku':'SKU-1','productId':'product-1','shopId':'shop-1','currency':'PHP',
            'priceIncludeVat':'87.19','marketPrice':'34.88','discount':'60','logisticsCost':'5'}]}]}
    monkeypatch.setattr(module,'capture_platform_controls',lambda *args,**kwargs:raw)
    calls=[]
    def capture(*args,**kwargs):calls.append(args);return official
    monkeypatch.setattr('app.adapters.official_price_logs.capture_official_price_logs',capture)
    result=adapter.lookup('execution-1',None,KEY)
    assert result.status=='accepted' and len(calls)==1
    assert result.price_record['prices']['actual_tax_inclusive']['amount']=='87.19'
    assert result.price_record['prices']['marketing']['amount']=='34.88'


def test_reconcile_does_not_store_another_executions_price_record(execution_db,tmp_path):
    from app.services.execution import PricingService
    from app.adapters.base import LookupResult
    from app.models import Execution,VariantGuard
    from app.services.store import utcnow
    record=record_result();record['execution_id']='another-execution'
    with execution_db.begin() as s:
        e=s.get(Execution,'execution-1');e.state='unknown';e.evidence={}
        s.get(VariantGuard,KEY.target_identity()).execution_id=e.id
    class Lookup:
        mode='live'
        def lookup(self,*args):return LookupResult('accepted',Decimal('34.88'),utcnow(),price_record=record)
    PricingService(execution_db,Lookup(),Settings(_env_file=None,runtime_dir=tmp_path,coordination_required=False)).reconcile()
    with execution_db() as s:
        e=s.get(Execution,'execution-1')
        assert e.state=='unknown' and 'price_record' not in e.evidence


@pytest.mark.parametrize('status', [401, 403, 429])
def test_recent_batches_share_interval_and_stop_on_http_rejection(tmp_path, monkeypatch, status):
    from app.adapters import sales_auth
    from app.adapters.official_price_logs import capture_recent_price_changes
    clock = [0]
    monkeypatch.setattr(sales_auth, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(sales_auth, 'sleep', sleep)
    monkeypatch.setattr(sales_auth.SalesAuth, 'get_token', lambda self: 'TEST')
    keys = [KEY.model_copy(update={'item_id':f'I{i:03d}'}) for i in range(201)]
    calls = []
    def handler(request):
        batch = json.loads(request.content)['sellerSkuIds']
        calls.append((clock[0], len(batch)))
        if len(calls) == 2:
            return httpx.Response(status)
        return httpx.Response(200, json={'resp_code':0,'datas':[
            {'sellerSkuId':sku,'count':0} for sku in batch]})
    settings = Settings(_env_file=None,runtime_dir=tmp_path,sales_request_interval_seconds=2)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_recent_price_changes(settings, keys, client=client, now=NOW)
    assert calls == [(0,100),(2,100)]
    assert result['records']['I000'] == {'status':'ok','count':0}
    assert result['records']['I100'] == result['records']['I200'] == {'status':'unavailable','count':None}
    assert result['stopped_http_status'] == status
