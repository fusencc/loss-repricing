import json
from pathlib import Path
from hashlib import sha256

import httpx
import pytest

from app.config import Settings


def test_catalog_reads_all_pages_but_does_not_use_unwindowed_sales(tmp_path):
    from app.adapters.erp_sku_catalog import capture_sku_catalog
    def handler(request):
        if request.url.path.endswith('/login'):
            return httpx.Response(200,json={'success':True,'code':200,'data':{'accessToken':'SECRET'}})
        assert request.url.path=='/open/goods/queryGoodsSales'
        assert request.headers['Authorization']=='Bearer SECRET'
        body=json.loads(request.content)
        assert body['skuList']==['S1','S2']
        page=body['pageNum']
        return httpx.Response(200,json={'success':True,'code':200,'total':2,'hasNext':page==1,
            'data':[{'sku':'S'+str(page),'mainSku':'M1','orderNum':999999,'singleDaySales':999999}]})
    settings=Settings(_env_file=None,runtime_dir=tmp_path,erp_username='TEST',erp_password='SECRET')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_sku_catalog(settings,['S1','S2','S1'],erp_capture_id='C1',synthetic=True,
                                  sample_only=False,client=client,page_size=1)
    assert result['acquisition_complete'] and result['missing_skus']==[]
    assert result['mapping']=={'S1':'M1','S2':'M1'}
    assert all('orderNum' not in r and 'singleDaySales' not in r for r in result['records'])
    for entry in result['files']:
        raw=Path(entry['file']).read_bytes()
        assert sha256(raw).hexdigest()==entry['sha256']
    assert 'SECRET' not in Path(result['artifact_file']).read_text()


@pytest.mark.parametrize('bad',['outside','duplicate','drift'])
def test_bad_catalog_scope_is_archived_without_promoting_a_parent(tmp_path,bad):
    from app.adapters.erp_sku_catalog import capture_sku_catalog
    def handler(request):
        if request.url.path.endswith('/login'):
            return httpx.Response(200,json={'success':True,'code':200,'data':{'accessToken':'SECRET'}})
        body=json.loads(request.content);page=body['pageNum']
        sku='OUTSIDE' if bad=='outside' else 'S1' if bad=='duplicate' else 'S'+str(page)
        return httpx.Response(200,json={'success':True,'code':200,'total':3 if bad=='drift' and page==2 else 2,
            'hasNext':page==1,'data':[{'sku':sku,'mainSku':'M1'}]})
    settings=Settings(_env_file=None,runtime_dir=tmp_path,erp_username='TEST',erp_password='SECRET')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_sku_catalog(settings,['S1','S2'],erp_capture_id='C1',synthetic=True,
                                  sample_only=False,client=client,page_size=1)
    assert not result['acquisition_complete'] and result['mapping']=={}
    assert result['files'] and result['issues']
