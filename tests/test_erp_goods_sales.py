import json
import httpx
import pytest
from app.adapters.erp_goods_sales import capture_goods_sales
from app.config import Settings


def run(tmp_path, rows, *, business_error=False):
    calls=[]
    def reply(request):
        calls.append((request.url.path,json.loads(request.content)))
        return httpx.Response(200,json={'code':500 if business_error else 200,
            'success':not business_error,'data':rows,'total':len(rows),'hasNext':False})
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        result=capture_goods_sales(Settings(_env_file=None,runtime_dir=tmp_path,erp_token='test'),
            {'capture_id':'C1','synthetic':True},[{'erp_sku':'A'},{'erp_sku':'B'},{'erp_sku':'A'}],
            client=client,interval_seconds=0)
    return result,calls


def row(sku='A',quantity=123):
    return {'sku':sku,'mainSku':'M','storageSalesList':[{'sku':sku,'storageName':'WH',
        'storageEntityName':'ENTITY','sevenDays':quantity}]}


def test_batches_unique_candidates_once_without_store_scan(tmp_path):
    result,calls=run(tmp_path,[row(),row('B',0)])
    assert len(calls)==1 and calls[0][0].endswith('/goods/queryGoodsSales')
    assert calls[0][1]['skuList']==['A','B']
    assert result['acquisition_complete']
    assert result['records']['A']['warehouses'][0]['quantity']==123
    assert result['records']['B']['warehouses'][0]['quantity']==0
    assert result['policy']=='reference_only'


@pytest.mark.parametrize('quantity',[None,-1,True,1.2])
def test_bad_reference_preserves_unknown_without_fabricating_zero(tmp_path,quantity):
    result,_=run(tmp_path,[row(quantity=quantity)])
    assert result['records']['A']['status']=='invalid_response'
    assert result['records']['A']['warehouses'][0]['quantity'] is None
    assert result['records']['B']['status']=='not_found'
    assert not result['acquisition_complete']


def test_business_failure_is_archived_as_unavailable(tmp_path):
    result,_=run(tmp_path,[],business_error=True)
    assert all(r['status']=='unavailable' for r in result['records'].values())
    assert result['issues']


def test_empty_candidates_make_no_http_requests(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda request:pytest.fail('unexpected HTTP'))) as client:
        result=capture_goods_sales(Settings(_env_file=None,runtime_dir=tmp_path),
            {'capture_id':'C1','synthetic':True},[],client=client)
    assert result['requested_skus']==[] and result['records']=={}
