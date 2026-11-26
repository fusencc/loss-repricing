import json
from pathlib import Path
import httpx
import pytest
from app.config import Settings


def run(tmp_path, mode='valid'):
    from app.adapters.erp_order_headers import capture_order_headers
    def handler(request):
        if request.url.path=='/newIp/system/login':
            return httpx.Response(200,json={'code':200,'data':{'accessToken':'SECRET'}})
        body=json.loads(request.content)
        assert body['filter1']=='r.orderid' and body['filter1value']=='O1\nO2'
        assert 'manyStatus' not in body and 'starttime' not in body
        rows=[{'orderid':i,'shopid':'STABLE','shopname':'SHOPPH','platform':'Tiktok',
               'orderNum':1,'sellNum':1,'customerEmail':'DO-NOT-ARCHIVE'} for i in ('O1','O2')]
        if mode=='wrong': rows[1]['orderid']='OUTSIDE'
        if mode=='missing': rows.pop()
        if mode=='duplicate': rows.append(dict(rows[1]))
        return httpx.Response(200,json={'code':0,'data':{'records':rows,'total':99 if mode=='total' else len(rows)}})
    settings=Settings(_env_file=None,runtime_dir=tmp_path,erp_username='U',erp_password='P')
    capture={'capture_id':'C1','synthetic':True,'sample_only':False,
             'groups':[{'candidate_order_ids':['O2','O1']}]}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return capture_order_headers(settings,capture,client=client)


def test_explicit_order_headers_archive_stable_owners_without_private_fields(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('订单归属不应回读文件计算哈希'))
        result=run(tmp_path)
    assert len(result['files']) == 1 and 'sha256' not in result['files'][0]
    assert result['acquisition_complete'] and set(result['orders']) == {'O1', 'O2'}
    assert result['orders']['O1']['shopid']=='STABLE'
    assert json.loads(Path(result['artifact_file']).read_text())==result
    assert all('DO-NOT-ARCHIVE' not in p.read_text() for p in tmp_path.rglob('*.json'))


@pytest.mark.parametrize('mode',['wrong','missing','duplicate'])
def test_only_unverified_orders_are_excluded_from_headers(tmp_path,mode):
    result=run(tmp_path,mode)
    assert not result['acquisition_complete']
    assert result['issues'] == [{'code':'order_header_unverified','order_ids':['O2']}]
    assert set(result['orders']) == {'O1'}


def test_header_total_changes_do_not_drop_verified_orders(tmp_path):
    result=run(tmp_path,'total')
    assert result['acquisition_complete']
    assert set(result['orders']) == {'O1', 'O2'}


def test_header_rate_limit_only_skips_current_batch(tmp_path):
    from app.adapters.erp_order_headers import capture_order_headers
    calls = []

    def handler(request):
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'code': 200, 'data': {'accessToken': 'TOKEN'}})
        order = json.loads(request.content)['filter1value']
        calls.append(order)
        if order == 'O2':
            return httpx.Response(200, json={'code': 500003})
        row = {'orderid': order, 'shopid': 'SHOP', 'shopname': 'SHOPPH', 'platform': 'Tiktok'}
        return httpx.Response(200, json={'code': 0, 'data': {'records': [row], 'total': 1}})

    settings = Settings(_env_file=None, runtime_dir=tmp_path, erp_username='U', erp_password='P')
    capture = {'capture_id': 'C1', 'groups': [{'candidate_order_ids': ['O1', 'O2', 'O3']}]}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_order_headers(settings, capture, client=client, batch_size=1)
    assert calls == ['O1', 'O2', 'O3']
    assert result['rate_limit_observed'] and not result['acquisition_complete']
    assert set(result['orders']) == {'O1', 'O3'}
    assert [(i['order_ids'], i['business_code']) for i in result['issues']] == [(['O2'], 500003)]
