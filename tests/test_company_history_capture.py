import json
from pathlib import Path
import httpx
from app.config import Settings
from test_company_price_history import case


def inputs():
    target,row,receipt,control=case()
    flags={'erp_capture_id':'C1','synthetic':True,'sample_only':False,
        'queried_at':'2026-11-11T02:00:00+00:00','completed_at':'2026-11-11T02:01:00+00:00'}
    capture={'capture_id':'C1','synthetic':True,'sample_only':False,'queried_at':'2026-11-11T00:00:00+00:00'}
    company={'schema_version':'sales-company-price-history-v1','source_id':'H1','sales_source_id':'S1',**flags,
        'records':[row],'requested_shops':['SHOPPH'],'company_log_complete':True,
        'window_start':'2026-11-08T02:02:00+00:00','window_end':'2026-11-11T02:02:00+00:00'}
    platform={'schema_version':'sales-platform-controls-v1','source_id':'P1',**flags,'records':[control]}
    prior={'schema_version':'local-live-price-history-v1','source_id':'L1',**flags,
        'acquisition_complete':True,'archive_binding':'injected_synthetic_test','company_observations':[]}
    return capture,target,row,receipt,company,platform,prior


def run(tmp_path,*,persisted=False,wrong_echo=False,confirmed_then_lower=False):
    from app.adapters.company_price_history import capture_company_history_effects
    c,t,row,receipt,h,p,prior=inputs();calls=[]
    if persisted:
        h['records']=[]
        prior['company_observations']=[{'state':'unverified','key':t['key'],'company_context':row,'event_key':'OLD'}]
    if confirmed_then_lower:
        from app.adapters.company_price_history import classify_company_event
        confirmed=classify_company_event(row,receipt,target=t,control=p['records'][0])['observation']
        prior['company_observations']=[confirmed]
        p['records'][0]['data']['skus'][0]['price']['tax_exclusive_price']='9'
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'access_token':'SECRET','expires_in':3600})
        assert request.method=='GET' and request.url.params['id']=='LOG1';calls.append(request.url.path)
        return httpx.Response(200,json={'resp_code':0,'datas':{'id':'OTHER' if wrong_echo else None,
            'requestBody':json.dumps(receipt['request_body']),'responseResult':json.dumps(receipt['platform_response']),
            'debug_headers':{'Authorization':'MUST-NOT-ARCHIVE-SECRET'}}})
    settings=Settings(_env_file=None,runtime_dir=tmp_path,vendor_username='U',vendor_password='P')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_company_history_effects(settings,c,[t],h,p,prior,client=client)
    return result,calls


def test_company_receipts_are_archived_redacted_and_bound_to_exact_ids(tmp_path):
    result,calls=run(tmp_path)
    assert len(calls)==1 and result['acquisition_complete']
    assert result['effects'][0]['history_known']
    assert result['observations'][0]['state']=='confirmed'
    assert result['observations'][0]['evidence']['company_context_source']=='H1'
    assert result['receipts']['LOG1']['requested_log_id']=='LOG1'
    assert json.loads(Path(result['artifact_file']).read_text())==result
    assert all('MUST-NOT-ARCHIVE-SECRET' not in path.read_text() for path in tmp_path.rglob('*.json'))


def test_old_unverified_event_is_rechecked_after_leaving_the_company_query_window(tmp_path):
    result,calls=run(tmp_path,persisted=True)
    assert len(calls)==1 and result['observations'][0]['state']=='confirmed'
    assert result['effects'][0]['confirmed_event_keys']


def test_conflicting_receipt_echo_never_confirms_a_target(tmp_path):
    result,_=run(tmp_path,wrong_echo=True)
    assert not result['effects'][0]['history_known'] and not result['observations']


def test_previously_confirmed_increase_is_not_downgraded_after_later_lower_price(tmp_path):
    result,_=run(tmp_path,confirmed_then_lower=True)
    effect=result['effects'][0]
    assert effect['history_known'] and not effect['pending_submission']
    assert effect['confirmed_event_keys'] and not effect['unverified_event_keys']
    assert not result['observations']  # 已确认事件的首次时间由权威档案保留。
