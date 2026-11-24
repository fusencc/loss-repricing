"""每日唯一费用来源：合成外部响应，验证持久缓存及属性匹配。"""
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.adapters import current_pricing_fees as fees
from app.adapters.sales_fee_model import compile_direct_fee


def fixtures():
    return {
        "overseas": [{"id": 1, "blocName": "GROUP", "authType": 1, "siteCode": "MY",
                      "platformRate": "17.28", "taxVatRate": "0.0001"}],
        "departments": [dict(id=i+10, authType=2, blocName="GROUP", departmentName="D1",
            siteCode="PH", mainlandId=m, shop3pf=1, returnRate="9", marketingRate=v,
            otherRate="3", packageFee="0.3", fixedFreight="0") for i,(m,v) in enumerate([(1,"6"),(0,"5")])],
        "logistics": [dict(id=i+20, blocName="GROUP", siteCode="PH", mainlandId=m, shop3pf=1,
            priceType=1, firstWeight=10, firstAmount=v, secondWeight=5, secondAmount="2",
            fixedFreight="0") for i,(m,v) in enumerate([(1,"5"),(0,"3")])],
        "shops": [dict(id=30+i, blocName="GROUP", shopName=n, site="PH", mainlandId=m,
                      shop3pf=1, property="铺货店铺") for i,(n,m) in enumerate([("SHOPPH",1),("LOCALPH",0)])],
        "public": [dict(id=40+t, platform="TIKTOK", site="PH", shopProperty="全部",
            shopMainland=-1, shop3pf=-1, rateType=t, associationCategory=False,
            rateFormat=1 if t==4 else 0, currency="RMB" if t==4 else "", expense=v)
            for t,v in [(0,"0"),(1,"2.24"),(2,"7.3"),(4,"1.3")]],
    }


def setup(tmp_path):
    settings=Settings(_env_file=None, runtime_dir=tmp_path, vendor_username="test", vendor_password="test",
                      erp_capture_bloc="GROUP", sales_request_interval_seconds=0)
    data=fixtures();calls=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(200,json={"access_token":"SECRET_TEST_TOKEN","expires_in":3600})
        names={fees.OVERSEAS:"overseas",fees.DEPARTMENT:"departments",fees.LOGISTICS:"logistics",
               fees.SHOPS:"shops",fees.PUBLIC:"public"}
        name=names.get(request.url.path)
        assert name is not None, "不允许其他费用查询路径"
        if name in data.get("_invalid",[]):return httpx.Response(200,content=b"null")
        if data[name] is None:return httpx.Response(503)
        body=json.loads(request.content)
        if name=="departments":assert body["authType"]==2
        rows=data[name]
        if name=="shops":return httpx.Response(200,json={"resp_code":0,"datas":rows})
        limit=body["limit"];start=(body["page"]-1)*limit
        payload={"code":0,"count":len(rows),"data":rows[start:start+limit]}
        if name not in ("overseas","public"):payload={"resp_code":0,"datas":payload}
        return httpx.Response(200,json=payload)
    return settings,data,calls,httpx.Client(transport=httpx.MockTransport(handler))


def at(day):return datetime(2026,11,day,8,tzinfo=ZoneInfo("Asia/Shanghai"))

def source(settings, client, capture_id="C1"):
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    daily = fees.refresh_daily_fees(settings, client=client, now=now, synthetic=True)
    return fees.capture_current_fees(settings, {'capture_id': capture_id, 'synthetic': True,
                                     'queried_at': now.isoformat()}, daily=daily)


def test_daily_cache_persists_restart_and_keeps_unchanged_version(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    first=fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True)
    count=len(calls)
    same=fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True)
    assert len(calls)==count and same["version"]==first["version"]
    assert same["artifact_file"]==first["artifact_file"]
    next_day=fees.refresh_daily_fees(settings,client=client,now=at(24),synthetic=True)
    assert len(calls)>count and next_day["version"]==first["version"]
    assert next_day["artifact_file"]==first["artifact_file"]
    assert next_day["checked_on"]=="2026-11-24"
    data["departments"][0]["marketingRate"]="4"
    changed=fees.refresh_daily_fees(settings,client=client,now=at(25),synthetic=True)
    assert changed["version"]!=first["version"]
    assert "SECRET_TEST_TOKEN" not in open(changed["artifact_file"]).read()


def test_failed_today_retries_only_missing_scopes_and_never_uses_yesterday(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True)
    departments = data['departments']
    data['departments'] = None
    failed = fees.refresh_daily_fees(settings,client=client,now=at(24),synthetic=True)
    assert not failed['acquisition_complete'] and failed['raw']['departments'] == []
    assert failed['raw']['overseas']
    data['departments'] = departments
    data['overseas'][0]['platformRate'] = '99'  # 已成功范围同日不重抓。
    calls.clear()
    recovered = fees.refresh_daily_fees(settings,client=client,now=at(24).replace(hour=9),synthetic=True)
    assert recovered['acquisition_complete'] and recovered['issues'] == []
    assert recovered['raw']['departments'] == departments
    assert recovered['raw']['overseas'] == failed['raw']['overseas']
    assert recovered['scope_checked_at']['overseas'] == failed['checked_at']
    assert recovered['scope_checked_at']['departments'] == recovered['checked_at']
    assert [p for p in calls if not p.endswith('/token')] == [fees.DEPARTMENT]
    assert set(f['file'] for f in failed['files']) < set(f['file'] for f in recovered['files'])
    count = len(calls)
    assert fees.refresh_daily_fees(settings,client=client,now=at(24),synthetic=True) == recovered
    assert len(calls) == count


def test_department_and_logistics_match_mainland_and_have_no_old_table_dependency(tmp_path,monkeypatch):
    settings,data,calls,client=setup(tmp_path)
    current=source(settings,client)
    assert current["acquisition_complete"]
    parent={"department":"D1","shopName":"SHOPPH","mainlandId":1}
    rate=fees.select_current_rate(current,{},parent,"PH","direct")
    assert rate["marketingRate"]=="6"
    assert rate["freight"]["base_price"]=="5"
    assert rate["freight"]["step_weight_g"]=="5"
    variant={"currency":"PHP","overseasStockSum":0,"marketPrice":"100","localWeight":"21","costPrice":"2"}
    compiled=compile_direct_fee(variant,rate,site="PH",company_cny_per_currency="0.1")
    assert Decimal(compiled["proof"]["freight_station"])==Decimal("11")
    local=fees.select_current_rate(current,{},dict(parent,shopName="LOCALPH",mainlandId=0),"PH","direct")
    assert local["marketingRate"]=="5" and local["freight"]["base_price"]=="3"
    second=source(settings,client,"C2")
    assert second["erp_capture_id"]=="C2" and current["config"]["version"]==second["config"]["version"]
    assert len([p for p in calls if p==fees.DEPARTMENT])==1


def test_duplicate_scope_does_not_choose_first_and_other_scope_survives(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    data["departments"].append(dict(data["departments"][0],id=999))
    current=source(settings,client)
    with pytest.raises(ValueError,match="唯一"):
        fees.select_current_rate(current,{}, {"department":"D1","shopName":"SHOPPH","mainlandId":1},"PH","direct")
    assert fees.select_current_rate(current,{}, {},"MY","overseas")["platformRate"]=="17.28"


def test_public_specificity_and_ambiguity(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    data["public"].append(dict(data["public"][2],id=99,shopProperty="铺货店铺",expense="8"))
    current=source(settings,client)
    parent={"department":"D1","shopName":"SHOPPH","mainlandId":1}
    assert fees.select_current_rate(current,{},parent,"PH","direct")["commissionRate"]=="8"
    # 同优先级两条配置不能随意挑一条，必须使该范围不可用。
    data["public"].append(dict(data["public"][-1],id=100,expense="9"))
    from app.adapters.current_pricing_fees import _public_for
    with pytest.raises(ValueError,match="唯一"):
        _public_for(data["public"],data["shops"][0],"PH")


def test_parent_shop_attribute_conflict_is_rejected(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    current=source(settings,client)
    with pytest.raises(ValueError,match="属性"):
        fees.select_current_rate(current,{}, {"department":"D1","shopName":"SHOPPH","mainlandId":0},"PH","direct")


def test_concurrent_startup_queries_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    settings,data,calls,client=setup(tmp_path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True),range(3)))
    assert len({r['version'] for r in results})==1
    assert sum(path==fees.DEPARTMENT for path in calls)==1


@pytest.mark.parametrize('change',['fixed_freight','weight_range','duplicate_template','missing_template'])
def test_unsupported_logistics_never_reads_old_workbook(tmp_path,change):
    settings,data,calls,client=setup(tmp_path)
    if change=='fixed_freight':data['logistics'][0]['fixedFreight']='60'
    elif change=='weight_range':data['logistics'][0]['priceType']=2
    elif change=='duplicate_template':data['logistics'].append(dict(data['logistics'][0],id=999))
    else:data['logistics']=[]
    current=source(settings,client)
    with pytest.raises(ValueError,match='物流'):
        fees.select_current_rate(current,{}, {'department':'D1','shopName':'SHOPPH','mainlandId':1},'PH','direct')


def test_cache_detects_corrupted_version_and_separates_synthetic_environment(tmp_path):
    from pathlib import Path
    from app.adapters.base import SourceUnavailable
    settings,data,calls,client=setup(tmp_path)
    one=fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True)
    two=fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=False)
    assert one['artifact_file']!=two['artifact_file']
    Path(one['artifact_file']).write_text('{}')
    with pytest.raises(SourceUnavailable,match='校验'):
        fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True)


def test_workflow_refreshes_daily_fees_before_erp_collection(tmp_path,monkeypatch):
    from app.services import erp_workflow
    settings,_,_,_=setup(tmp_path)
    events=[]
    monkeypatch.setattr(erp_workflow,'refresh_daily_fees',lambda s:events.append('fees'))
    def stop(*args,**kwargs):
        events.append('erp')
        raise RuntimeError('stop after order proof')
    monkeypatch.setattr(erp_workflow,'capture_erp_bulk',stop)
    with pytest.raises(RuntimeError,match='order proof'):
        erp_workflow._run_workflow(settings,department='D1')
    assert events==['fees','erp']


def test_daily_config_is_bound_after_capture_without_falsifying_check_time(tmp_path):
    from datetime import timezone
    from app.adapters.erp_order_loss import _bound
    settings,data,calls,client=setup(tmp_path)
    captured = datetime.now(timezone.utc)
    checked = captured.astimezone(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0)
    daily=fees.refresh_daily_fees(settings,client=client,now=checked,synthetic=True)
    capture={'capture_id':'LATER','queried_at':captured.isoformat(),'synthetic':True,'sample_only':False}
    current=fees.capture_current_fees(settings,capture,daily=daily)
    _bound(capture,current,fees.SCHEMA)
    assert current['daily_source']['checked_at']==daily['checked_at']
    assert current['observation_kind']=='local_daily_config_binding'


def test_binding_rejects_wrong_business_day_or_environment(tmp_path):
    from app.adapters.base import SourceUnavailable

    settings,_,_,client=setup(tmp_path)
    daily = fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True)
    capture = {'capture_id':'CROSS','queried_at':at(24).isoformat(),'synthetic':True}
    with pytest.raises(SourceUnavailable, match='业务日'):
        fees.capture_current_fees(settings,capture,daily=daily)
    with pytest.raises(SourceUnavailable, match='运行环境'):
        fees.capture_current_fees(settings,dict(capture,queried_at=at(23).isoformat(),synthetic=False),daily=daily)


def test_nonobject_response_is_isolated_and_persisted_for_today(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    data['_invalid']=['departments']
    result=fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True)
    assert not result['acquisition_complete'] and result['raw']['departments']==[]
    assert result['raw']['overseas'] and result['raw']['logistics']
    calls.clear()
    retry = fees.refresh_daily_fees(settings,client=client,now=at(23),synthetic=True)
    assert not retry['acquisition_complete']
    assert [p for p in calls if not p.endswith('/token')] == [fees.DEPARTMENT]


def test_category_bound_public_fee_does_not_hide_generic(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    data['public'].append(dict(data['public'][2],id=99,shopProperty='铺货店铺',associationCategory=True,expense='20'))
    current=source(settings,client)
    parent = {'department':'D1','shopName':'SHOPPH','mainlandId':1}
    assert fees.select_current_rate(current,{},parent,'PH','direct')['commissionRate'] == '7.3'
    current['config']['public'] = [r for r in current['config']['public'] if r['id'] != 42]
    with pytest.raises(ValueError,match='类目'):
        fees.select_current_rate(current,{},parent,'PH','direct')


def test_auth_scope_filter_retains_valid_rows_across_pages(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    data['departments'][:0] = [dict(data['departments'][0], id=200+i, authType=5, blocName='OTHER')
                               for i in range(100)]
    data['overseas'].append(dict(data['overseas'][0], id=3, shopName='SHOP', authType=1))
    current = source(settings,client)
    assert current['acquisition_complete'] and current['issues'] == []
    assert [r['id'] for r in current['config']['departments']] == [10,11]
    assert len(current['config']['overseas']) == 1
    assert calls.count(fees.DEPARTMENT) == 2
    assert current['warnings'] == [
        {'scope':'overseas','code':'non_group_overseas_fee_scope','skipped_count':1},
        {'scope':'departments','code':'non_department_fee_scope','skipped_count':100},
    ]


def test_all_wrong_auth_scopes_remain_unavailable(tmp_path):
    settings,data,calls,client=setup(tmp_path)
    data['departments'] = [dict(r, authType=5) for r in data['departments']]
    current = source(settings,client)
    assert not current['acquisition_complete'] and current['config']['departments'] == []
    assert current['issues'] == [{'scope':'departments','code':'daily_departments_empty'}]
    assert current['warnings'][0]['skipped_count'] == 2
    assert current['config']['overseas']
