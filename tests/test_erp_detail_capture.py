from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import httpx
from openpyxl import Workbook
import pytest

from app.config import Settings


def config(tmp_path):
    return Settings(_env_file=None,runtime_dir=tmp_path,erp_username='TEST',erp_password='SECRET')


def test_shared_web_session_accepts_the_explicit_catalog_success_contract(tmp_path):
    from app.adapters.erp_bulk_capture import _session
    def handler(request):
        if request.url.path=='/newIp/system/login':
            return httpx.Response(200,json={'code':200,'data':{'accessToken':'SECRET-TOKEN'}})
        assert request.url.path=='/Api/order/findshop2/PLATFORM'
        return httpx.Response(200,json={'code':0,'data':[{'shopid':'ERP-SHOP','name':'SHOPPH'}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with _session(config(tmp_path),client) as (post,_):
            assert post('/Api/order/findshop2/PLATFORM',None,success_code=0)==[{'shopid':'ERP-SHOP','name':'SHOPPH'}]


def capture():
    return {'capture_id':'CAPTURE-TEST','synthetic':True,'sample_only':False,'groups':[
        {'candidate_order_ids':['O1','O2']},{'candidate_order_ids':['O1']}]}


def workbook(outside=False):
    w=Workbook();s=w.active
    s.append(['订单编号','平台','店铺','商品SKU列表','订单数量','SKU修正毛利'])
    s.append(['O9' if outside else 'O1','Tiktok','SHOP','S1',1,None])
    s.append(['O1','Tiktok','SHOP','S1',2,None])
    s.append(['O2','Tiktok','SHOP','S2',1,None])
    b=BytesIO();w.save(b);return b.getvalue()


def transport(outside=False,rate_limited=False):
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path=='/newIp/system/login':
            return httpx.Response(200,json={'code':200,'data':{'accessToken':'SECRET-TOKEN'}})
        assert request.headers.get('authorization')=='SECRET-TOKEN'
        if request.url.path=='/newIp/report/orderDownload/asyncOrderDetailDownload':
            ids=json.loads(request.content)['orderIdList']
            if rate_limited:return httpx.Response(200,json={'code':500003,'data':None})
            assert ids==['O1','O2']
            return httpx.Response(200,json={'code':200,'data':'下载任务【DETAIL-TEST】'})
        if request.url.path=='/newIp/report/download/list':
            assert json.loads(request.content)['source']=='orderDetailList'
            return httpx.Response(200,json={'code':200,'data':{'records':[
                {'source':'orderDetailList','fileName':'DETAIL-TEST','id':'TASK-1','status':'success','downloadUrl':'/detail-file'}]}})
        if request.url.path=='/detail-file':return httpx.Response(200,content=workbook(outside))
        pytest.fail('Unexpected request: '+request.url.path)
    return httpx.MockTransport(handler),calls


def test_downloads_unique_candidate_orders_and_preserves_physical_rows(tmp_path):
    from app.adapters.erp_detail_capture import capture_erp_details
    mock,calls=transport()
    with httpx.Client(transport=mock) as client:
        result=capture_erp_details(config(tmp_path),capture(),client=client)
    assert result['acquisition_complete']
    assert result['requested_order_ids']==['O1','O2']
    assert len(result['rows'])==3
    assert len({r['ref'] for r in result['rows']})==3
    assert [r['fields']['订单数量'] for r in result['rows']]==[1,2,1]
    assert result['rows'][0]['fields']['SKU修正毛利'] is None
    assert result['erp_capture_id']=='CAPTURE-TEST' and result['synthetic'] is True
    stored=json.loads(Path(result['artifact_file']).read_text())
    assert stored==result and 'SECRET' not in json.dumps(stored)
    original=result['files'][0];raw=Path(original['file']).read_bytes()
    assert sha256(raw).hexdigest()==original['sha256']


def test_order_details_share_the_owned_web_session_and_do_not_login_again(tmp_path):
    from app.adapters.erp_bulk_capture import _session
    from app.adapters.erp_detail_capture import capture_erp_details
    mock,calls=transport()
    with httpx.Client(transport=mock) as client:
        with _session(config(tmp_path),client) as web_session:
            result=capture_erp_details(config(tmp_path),capture(),web_session=web_session)
    assert result['acquisition_complete']
    assert calls.count('/newIp/system/login')==1


def test_default_wait_budget_finishes_a_slow_task_without_resubmitting(tmp_path):
    from app.adapters.erp_detail_capture import capture_erp_details
    base,calls=transport();polls=[]
    def handler(request):
        response=base.handle_request(request)
        if request.url.path=='/newIp/report/download/list':
            payload=response.json();polls.append(1)
            if len(polls)<37:payload['data']['records'][0]['status']='waiting'
            return httpx.Response(200,json=payload)
        return response
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_erp_details(config(tmp_path),capture(),client=client)
    assert result['acquisition_complete'] and len(polls)==37
    assert calls.count('/newIp/report/orderDownload/asyncOrderDetailDownload')==1


def test_wait_budget_exhaustion_preserves_pending_task_and_stops_new_exports(tmp_path):
    from app.adapters.erp_detail_capture import capture_erp_details
    submissions=[]
    def handler(request):
        if request.url.path=='/newIp/system/login':
            return httpx.Response(200,json={'code':200,'data':{'accessToken':'TOKEN'}})
        if request.url.path=='/newIp/report/orderDownload/asyncOrderDetailDownload':
            submissions.append(json.loads(request.content)['orderIdList'])
            return httpx.Response(200,json={'code':200,'data':'下载任务【SLOW-TASK】'})
        if request.url.path=='/newIp/report/download/list':
            return httpx.Response(200,json={'code':200,'data':{'records':[
                {'source':'orderDetailList','fileName':'SLOW-TASK','id':'TASK-ID','status':'waiting'}]}})
        pytest.fail('Unfinished task cannot be downloaded')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_erp_details(config(tmp_path),capture(),client=client,batch_size=1,max_polls=2)
    assert submissions==[['O1']]
    assert result['stopped_by_pending_task'] and not result['acquisition_complete']
    assert result['batches'][0]['status']=='pending' and result['batches'][0]['task_id']=='TASK-ID'
    assert result['batches'][1]['status']=='not_attempted'


def test_unrequested_order_is_archived_but_not_accepted(tmp_path):
    from app.adapters.erp_detail_capture import capture_erp_details
    mock,calls=transport(outside=True)
    with httpx.Client(transport=mock) as client:
        result=capture_erp_details(config(tmp_path),capture(),client=client)
    assert not result['acquisition_complete'] and not result['rows']
    assert result['files'] and any(i['code']=='detail_scope_mismatch' for i in result['issues'])


def test_rate_limit_stops_remaining_batches_without_retry(tmp_path):
    from app.adapters.erp_detail_capture import capture_erp_details
    mock,calls=transport(rate_limited=True)
    with httpx.Client(transport=mock) as client:
        result=capture_erp_details(config(tmp_path),capture(),client=client,batch_size=1)
    assert not result['acquisition_complete'] and result['stopped_by_rate_limit']
    assert calls.count('/newIp/report/orderDownload/asyncOrderDetailDownload')==1
    assert result['batches'][1]['status']=='not_attempted'
