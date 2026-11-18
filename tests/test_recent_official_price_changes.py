from datetime import datetime, timedelta, timezone
import json
import httpx
import pytest
from app.config import Settings
from app.domain.types import VariantKey
from app.adapters import official_price_logs

NOW=datetime(2026,11,17,8,52,42,tzinfo=timezone.utc)
KEY=VariantKey(platform='TikTok',shop_id='S',site='PH',product_id='P',item_id='I1',sku='SKU',warehouse='WH')

@pytest.mark.parametrize('count',[0,3])
def test_recent_count_uses_exact_beijing_72_hour_window_and_unique_child_ids(tmp_path,monkeypatch,count):
    monkeypatch.setattr(official_price_logs.SalesAuth,'get_token',lambda self:'secret')
    calls=[]
    def handle(req):
        calls.append(req)
        assert req.url.path==official_price_logs.COUNT_PATH
        assert json.loads(req.content)=={'sellerSkuIds':['I1'],'type':2,
            'completeTimeStart':'2026-11-14 16:52:42','completeTimeEnd':'2026-11-17 16:52:42'}
        return httpx.Response(200,json={'resp_code':0,'datas':[{'sellerSkuId':'I1','count':count}]})
    result=official_price_logs.capture_recent_price_changes(Settings(_env_file=None,runtime_dir=tmp_path),[KEY,KEY],
        now=NOW,client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert len(calls)==1
    assert result['records']['I1']['count']==count
    assert result['records']['I1']['status']=='ok'
    assert 'secret' not in json.dumps(result)

@pytest.mark.parametrize('rows',[
    [],[{'sellerSkuId':'OTHER','count':0}],[{'sellerSkuId':'I1','count':True}],
    [{'sellerSkuId':'I1','count':-1}],[{'sellerSkuId':'I1','count':0}]*2,
])
def test_missing_wrong_or_invalid_count_never_becomes_zero(tmp_path,monkeypatch,rows):
    monkeypatch.setattr(official_price_logs.SalesAuth,'get_token',lambda self:'token')
    result=official_price_logs.capture_recent_price_changes(Settings(_env_file=None,runtime_dir=tmp_path),[KEY],now=NOW,
        client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'resp_code':0,'datas':rows}))))
    assert result['records']['I1']['count'] is None
    assert result['records']['I1']['status']!='ok'


def test_engine_blocks_official_count_without_inventing_last_effective_time():
    from test_pricing import single,NOW as now
    decision=single(online_fields={'recent_price_log':{'count':3,'window_start':now-timedelta(hours=72),
        'window_end':now,'source_id':'official-test'},'last_increase_at':None})
    assert decision.reason_code=='recent_price_change' and not decision.ready
    assert decision.status=='本轮不调'


def test_live_service_overrides_empty_local_archive_with_official_count(tmp_path,monkeypatch):
    from test_execution_safety import SyntheticAdapter
    from app.db import Base,create_database
    from app.services.execution import PricingService
    clock=[NOW];adapter=SyntheticAdapter(clock,mode='live')
    engine,factory=create_database('sqlite://');Base.metadata.create_all(engine)
    calls=[]
    def capture(settings,keys,**kwargs):
        calls.append(keys)
        return {'source_id':'official','window_start':(NOW-timedelta(hours=72)).isoformat(),
            'window_end':NOW.isoformat(),'records':{k.item_id:{'count':3 if k.item_id=='I0' else 0,'status':'ok'} for k in keys}}
    monkeypatch.setattr('app.services.execution.capture_recent_price_changes',capture,raising=False)
    result=PricingService(factory,adapter,Settings(_env_file=None,runtime_dir=tmp_path))._enrich(adapter.collect(NOW.date()))
    assert calls
    assert result.online[0].recent_price_log.count==3
    assert result.online[0].last_increase_at is None
    engine.dispose()

@pytest.mark.parametrize('failure',['business','timeout','invalid_json'])
def test_query_failure_never_allows_pricing(tmp_path,monkeypatch,failure):
    monkeypatch.setattr(official_price_logs.SalesAuth,'get_token',lambda self:'token')
    def handle(req):
        if failure=='timeout':raise httpx.ReadTimeout('private text',request=req)
        if failure=='invalid_json':return httpx.Response(200,content=b'not-json')
        return httpx.Response(200,json={'resp_code':1,'resp_msg':'private text'})
    result=official_price_logs.capture_recent_price_changes(Settings(_env_file=None,runtime_dir=tmp_path),[KEY],now=NOW,
        client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert result['records']['I1']['count'] is None
    assert not result['acquisition_complete']
    assert 'private text' not in json.dumps(result)


def test_partial_batch_retains_independent_valid_counts(tmp_path,monkeypatch):
    monkeypatch.setattr(official_price_logs.SalesAuth,'get_token',lambda self:'token')
    other=KEY.model_copy(update={'item_id':'I2'})
    result=official_price_logs.capture_recent_price_changes(Settings(_env_file=None,runtime_dir=tmp_path),[KEY,other],now=NOW,
        client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,
            json={'resp_code':0,'datas':[{'sellerSkuId':'I2','count':0}]}))))
    assert result['records']['I1']['count'] is None
    assert result['records']['I2']=={'status':'ok','count':0}


@pytest.mark.parametrize('count',[0,3,None])
def test_preparation_uses_official_history_and_keeps_missing_unknown(count):
    from test_live_online_preparation import sources
    from app.adapters.live_online_preparation import prepare_online_states
    capture,targets,inputs=sources()
    inputs['official_history']['records']['I1']={'status':'ok' if count is not None else 'transport_error','count':count}
    result=prepare_online_states(capture,targets,**inputs)
    if count is None:
        assert not result['online'] and '开放调价日志' in result['issues'][0]['reason']
    else:
        state=result['online'][0]['state']
        assert state['last_increase_at'] is None and state['recent_price_log']['count']==count


@pytest.mark.parametrize('count,expected_status',[(3,'本轮不调'),(None,'待处理')])
def test_pre_submit_rechecks_official_log_and_never_claims_on_positive_or_unknown(tmp_path,monkeypatch,count,expected_status):
    from test_execution_safety import SyntheticAdapter
    from app.services.execution import PricingService
    from app.domain.engine import evaluate
    adapter=SyntheticAdapter([NOW],mode='live')
    service=PricingService(None,adapter,Settings(_env_file=None,runtime_dir=tmp_path))
    decision=evaluate(adapter.collect(NOW.date()),NOW)[0]
    monkeypatch.setattr(service,'_allowed',lambda *a,**k:(True,''))
    monkeypatch.setattr(service,'_claim',lambda *a:pytest.fail('must not claim or submit'))
    held=[]
    monkeypatch.setattr(service,'_hold',lambda *a,**k:held.append(a))
    monkeypatch.setattr('app.services.execution.capture_recent_price_changes',lambda *a,**k:{
        'source_id':'official','window_start':(NOW-timedelta(hours=72)).isoformat(),'window_end':NOW.isoformat(),
        'records':{'I0':{'count':count,'status':'ok' if count is not None else 'transport_error'}}})
    service._submit('item',decision)
    assert held and held[0][2]==expected_status


def test_failed_first_chunk_does_not_discard_second_chunk(tmp_path,monkeypatch):
    monkeypatch.setattr(official_price_logs.SalesAuth,'get_token',lambda self:'token')
    keys=[KEY.model_copy(update={'item_id':f'I{i:03d}'}) for i in range(101)]
    calls=[]
    def handle(req):
        ids=json.loads(req.content)['sellerSkuIds'];calls.append(ids)
        if len(ids)==100:return httpx.Response(503)
        return httpx.Response(200,json={'resp_code':0,'datas':[{'sellerSkuId':ids[0],'count':0}]})
    result=official_price_logs.capture_recent_price_changes(Settings(_env_file=None,runtime_dir=tmp_path),keys,now=NOW,
        client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert [len(ids) for ids in calls]==[100,1]
    assert result['records']['I000']=={'status':'transport_error','count':None}
    assert result['records']['I100']=={'status':'ok','count':0}


@pytest.mark.parametrize('field,value',[
    ('schema_version','wrong'),('synthetic',False),('basis','local_history'),
])
def test_preparation_rejects_wrong_official_source_contract(field,value):
    from test_live_online_preparation import sources
    from app.adapters.live_online_preparation import prepare_online_states
    capture,targets,inputs=sources()
    inputs['official_history'][field]=value
    with pytest.raises(ValueError,match='开放调价日志来源'):
        prepare_online_states(capture,targets,**inputs)
