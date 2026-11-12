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
        return httpx.Response(200,json={'code':0,'data':{'records':rows,'total':len(rows)}})
    settings=Settings(_env_file=None,runtime_dir=tmp_path,erp_username='U',erp_password='P')
    capture={'capture_id':'C1','synthetic':True,'sample_only':False,
             'groups':[{'candidate_order_ids':['O2','O1']}]}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return capture_order_headers(settings,capture,client=client)


def test_explicit_order_headers_archive_stable_owners_without_private_fields(tmp_path):
    result=run(tmp_path)
    assert result['acquisition_complete']
    assert set(result['orders'])=={'O1','O2'}
    assert result['orders']['O1']['shopid']=='STABLE'
    assert json.loads(Path(result['artifact_file']).read_text())==result
    assert all('DO-NOT-ARCHIVE' not in p.read_text() for p in tmp_path.rglob('*.json'))


@pytest.mark.parametrize('mode',['wrong','missing'])
def test_incomplete_or_outside_order_headers_never_prove_ownership(tmp_path,mode):
    result=run(tmp_path,mode)
    assert not result['acquisition_complete'] and result['issues']
    assert not result['orders']
