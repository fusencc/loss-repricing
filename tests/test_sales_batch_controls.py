import json
import httpx
from app.config import Settings
from app.adapters.sales_batch_controls import capture_batch_controls

class Auth:
    def collection_headers(self): return {}
    def observe_collection_response(self, response): pass

def test_batch_reuses_export_and_only_fetches_overseas_details(tmp_path):
    calls=[]
    def request(req):
        calls.append(req.url.path)
        if req.url.path.endswith('/findByPage'):
            return httpx.Response(200,json={'code':0,'count':2,'data':[
                {'shopName':'SHOP','shopId':'S','productId':p,'bloc':'B','localMainSku':'M','productStatus':4} for p in ['P1','P2']]})
        assert req.url.path.endswith('/findProductVariants/SHOP/P2')
        return httpx.Response(200,json={'resp_code':0,'datas':[{'shopName':'SHOP','productId':'P2','sellerSkuId':'I2','overseasStockWarehouseInfo':'[]'}]})
    variants=[]
    for n in [1,2]:
        variants.append({'shopName':'SHOP','shopId':'S','productId':f'P{n}','sellerSkuId':f'I{n}','sellerSku':f'SKU{n}','localSellerSku':f'SKU{n}',
            'batch_pricing':{'含vat价格':'100','营销价格':'80','折扣':'20','活动类型':'直接折扣','币种':'THB','海外仓库存':'0' if n==1 else '5','erp成本价':'3','包装重量':'10'}})
    sales={'source_id':'EXPORT','completed_at':'2026-11-27T00:00:00+00:00','variants':variants}
    targets=[{'erp_shop':'SHOP','product_id':f'P{n}','item_id':f'I{n}'} for n in [1,2]]
    cfg=Settings(_env_file=None,runtime_dir=tmp_path,erp_capture_bloc='B')
    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        result=capture_batch_controls(cfg,{'capture_id':'C','synthetic':True},targets,sales=sales,client=client,auth=Auth())
    assert len(result['records'])==2 and not result['issues']
    assert len(calls)==2
    direct=result['records'][0]
    assert direct['current_variants'][0]['priceIncludeVat']=='100'
    assert direct['current_variants'][0]['costPrice']=='3'
    assert 'status_info' not in direct['data']['skus'][0]
    assert result['source_mode']=='sales_batch_export_with_parent_list'
