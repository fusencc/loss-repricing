import json
from io import BytesIO
from pathlib import Path
from datetime import datetime
from zipfile import ZipFile

import httpx
from openpyxl import Workbook
import pytest

from app.config import Settings


def workbook(*, bad_date=False, bad_quantity=False):
    book=Workbook();sheet=book.active
    sheet.append(['订单编号','平台','店铺','商品SKU列表','订单数量','SKU修正毛利','订单付款时间','订单状态'])
    paid=datetime(2026,11,3 if bad_date else 10,12)
    sheet.append(['O1','Tiktok','SHOPPH','S1',1,None,paid,'已发货'])
    sheet.append(['O1','Tiktok','SHOPPH','S1',1 if bad_quantity else 2,None,paid,'已发货'])
    sheet.append(['O2','Tiktok','SHOPPH','S2',1,None,paid,'已发货'])
    stream=BytesIO();book.save(stream)
    # ERP的POI导出把整数件数写为1.0；保留真实Excel边界类型。
    output=BytesIO()
    with ZipFile(BytesIO(stream.getvalue())) as source,ZipFile(output,'w') as target:
        for item in source.infolist():
            raw=source.read(item.filename)
            if item.filename=='xl/worksheets/sheet1.xml':
                raw=raw.replace(b'<v>1</v>',b'<v>1.0</v>').replace(b'<v>2</v>',b'<v>2.0</v>')
            target.writestr(item,raw)
    return output.getvalue()


def run(tmp_path, *, empty=False, bad_date=False, bad_catalog=False, bad_quantity=False):
    from app.adapters.erp_sales_window import capture_sales_window
    calls=[]
    def handler(request):
        path=request.url.path;calls.append(path)
        if path=='/newIp/system/login':
            return httpx.Response(200,json={'code':200,'data':{'accessToken':'SECRET-TOKEN'}})
        if path.startswith('/Api/order/findshop2/'):
            return httpx.Response(200,json={'code':0,'data':[{'shopid':'SHOP-ID','name':'SHOPPH','platname':'Tiktok',
                'accessToken':'DO-NOT-ARCHIVE-SECRET','emailPassword':'DO-NOT-ARCHIVE-SECRET'}]})
        if path=='/Api/order/list':
            body=json.loads(request.content)
            assert body['shopid']=='SHOP-ID' and body['manyStatus']=='2,3,4,5,6,7'
            assert body['starttime']=='2026-11-04' and body['endtime']=='2026-11-10'
            rows=[] if empty else [{'orderid':i,'shopid':'SHOP-ID','shopname':'SHOPPH','platform':'Tiktok',
                'status':'6','orderTime':'2026-11-10 12:00:00','orderNum':3 if i=='O1' else 1,'sellNum':1} for i in ['O1','O2']]
            return httpx.Response(200,json={'code':0,'data':{'total':len(rows),'records':rows}})
        if path=='/newIp/report/orderDownload/asyncOrderDetailDownload':
            assert json.loads(request.content)['orderIdList']==['O1','O2']
            return httpx.Response(200,json={'code':200,'data':'下载任务【WINDOW-DETAIL】'})
        if path=='/newIp/report/download/list':
            return httpx.Response(200,json={'code':200,'data':{'records':[
                {'source':'orderDetailList','fileName':'WINDOW-DETAIL','id':'TASK','status':'success','downloadUrl':'/raw.xlsx'}]}})
        if path=='/raw.xlsx':return httpx.Response(200,content=workbook(bad_date=bad_date,bad_quantity=bad_quantity))
        if path=='/open/system/innerOpen/login':
            return httpx.Response(200,json={'code':200,'success':True,'data':{'accessToken':'SECRET-TOKEN'}})
        if path=='/open/goods/queryGoodsSales':
            skus=json.loads(request.content)['skuList']
            rows=[{'sku':sku,'mainSku':'WRONG' if bad_catalog else 'M1','orderNum':999999} for sku in skus]
            return httpx.Response(200,json={'code':200,'success':True,'data':rows,'total':len(rows),'hasNext':False})
        pytest.fail('Unexpected source request: '+path)
    settings=Settings(_env_file=None,runtime_dir=tmp_path,erp_username='TEST',erp_password='SECRET')
    capture={'capture_id':'C1','queried_at':'2026-11-11T01:00:00+00:00','synthetic':True,'sample_only':False}
    targets=[{'erp_shop':'SHOPPH','shop_id':'SHOP-ID','erp_sku':'S1','main_sku':'M1'}]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_sales_window(settings,capture,targets,client=client,catalog_client=client)
    return result,calls


def test_complete_window_reconciles_orders_native_rows_and_main_sku(tmp_path):
    result,calls=run(tmp_path)
    assert result['acquisition_complete'] and not result['issues']
    assert result['window_start']=='2026-11-04' and result['window_end']=='2026-11-10'
    assert result['volumes']==[{'erp_shop':'SHOPPH','main_sku':'M1','quantity':4,'complete':True}]
    assert len(result['rows'])==3 and len({r['native_ref'] for r in result['rows']})==3
    assert calls.count('/newIp/system/login')==1
    for file in tmp_path.rglob('*.json'):
        assert 'DO-NOT-ARCHIVE-SECRET' not in file.read_text()
    assert json.loads(Path(result['artifact_file']).read_text())==result


def test_confirmed_empty_window_produces_zero_after_catalog_identity_check(tmp_path):
    result,calls=run(tmp_path,empty=True)
    assert result['acquisition_complete'] and result['volumes'][0]['quantity']==0
    assert '/newIp/report/orderDownload/asyncOrderDetailDownload' not in calls


@pytest.mark.parametrize('case',['bad_date','bad_catalog','bad_quantity'])
def test_conflicting_window_source_never_becomes_zero_sales(tmp_path,case):
    result,_=run(tmp_path,**{case:True})
    assert not result['acquisition_complete'] and result['issues']
    assert result['volumes'][0]['quantity'] is None and not result['volumes'][0]['complete']
