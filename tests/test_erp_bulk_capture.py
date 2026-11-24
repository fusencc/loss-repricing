from pathlib import Path
"""批量合同回归：只读下载，无逐单查询、任务串单及范围扩大。"""
from io import BytesIO
import json
import zipfile
import httpx
import pytest
from openpyxl import Workbook
from app.config import Settings
from app.adapters.erp_bulk_capture import capture_erp_bulk


def xlsx(headers, rows, broken_dimension=False):
    w = Workbook(); s = w.active; s.append(headers)
    for row in rows: s.append(row)
    f = BytesIO(); w.save(f)
    if not broken_dimension: return f.getvalue()
    import re
    result = BytesIO()
    with zipfile.ZipFile(f) as source, zipfile.ZipFile(result, 'w') as target:
        for n in source.namelist():
            b = source.read(n)
            if n == 'xl/worksheets/sheet1.xml':
                b = re.sub(rb'<dimension ref="[^"]+"/>', b'<dimension ref="A1:A1"/>', b)
            target.writestr(n, b)
    return result.getvalue()


def run(tmp_path, *, issue=None, **capture_options):
    calls = []
    loss = xlsx(['订单编号','订单状态','平台','集团','毛利'],
                [['0001234567890123456789','可配货','Tiktok','集团二部',-1]], True)
    headers = ['订单编号','订单状态','平台','集团','订单金额','订单成本','毛利','修正毛利','修正一次毛利','包材费']
    row = ['OTHER' if issue == 'mismatch' else '0001234567890123456789',
           None if issue == 'null_status' else '已完成' if issue == 'status' else '可配货',
           'Shopee' if issue == 'platform' else 'Tiktok','集团二部',1.2345,2,-1,-2,-3,None]
    if issue == 'duplicate_column':
        headers.append('修正一次毛利')
        row.append(-999)
    order = xlsx(headers, [row, row] if issue == 'duplicate_finance' else [row])
    def handler(r):
        calls.append(r.url.path)
        p = r.url.path
        if p.endswith('/login'): return httpx.Response(200,json={'code':200,'success':True,'data':{'token':'SECRET'}})
        if p.endswith('/dowDeficitOrder'):
            assert json.loads(r.content)['financecheck'] == '3'
            return httpx.Response(200,content=loss)
        if p.endswith('/asyncOrderListDownload'):
            assert json.loads(r.content) == {'orderIdList':['0001234567890123456789']}
            return httpx.Response(200,json={'code':200,'data':'下载任务【精确任务】后台执行中'})
        if p.endswith('/download/list'):
            if issue == 'rate': return httpx.Response(200,json={'code':500003})
            return httpx.Response(200,json={'code':200,'data':[
                {'id':'wrong','fileName':'别人的最新任务','source':'orderList','status':'success','downloadUrl':'/wrong.xlsx'},
                {'id':'right','fileName':'精确任务','source':'orderList','status':'running' if issue == 'timeout' else 'success','downloadUrl':'/right.xlsx'}]})
        if p == '/right.xlsx': return httpx.Response(200,content=order)
        pytest.fail('unexpected request: '+p)
    settings = Settings(_env_file=None, runtime_dir=tmp_path, erp_username='u',erp_password='p')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_erp_bulk(settings, source="loss_allocatable", client=client, max_polls=2, **capture_options)
    return result, calls


def test_bulk_single_source_roundtrip_without_per_order_calls(tmp_path):
    result,calls = run(tmp_path)
    assert result['capture_complete'] and result['pricing_ready'] is False
    assert [(g['kind'],g['status']) for g in result['groups']] == [('loss',4)]
    assert result['summary']['unique_orders'] == 1
    row = result['groups'][0]['rows'][0]
    assert row['orderid'] == '0001234567890123456789'
    assert row['fields']['订单金额'] == '1.2345' and row['fields']['包材费'] is None
    assert result['groups'][0]['task_id'] == 'right'
    assert result['details'] == []
    assert not any('findByOrderId' in x or 'findDetail' in x or x.endswith('/list') and '/download/' not in x for x in calls)
    assert 'SECRET' not in json.dumps(result) and 'downloadUrl' not in json.dumps(result)
    assert len(list((tmp_path/'source-captures').rglob('*.xlsx'))) == 3


@pytest.mark.parametrize('issue',['mismatch','platform','timeout','rate'])
def test_bulk_failure_is_explicit_and_partial_source_is_preserved(tmp_path, issue):
    result,calls = run(tmp_path,issue=issue)
    assert result['capture_complete'] is False
    assert result['groups'][0]['error']
    assert len(result['groups'][0]['loss_rows']) == 1
    assert result['groups'][0]['rows'] == []
    assert result['stopped_by_rate_limit'] is (issue == 'rate')
    if issue == 'rate': assert calls.count('/newIp/report/download/list') == 1


def test_duplicate_finance_is_kept_for_audit_without_marking_capture_complete(tmp_path):
    from app.services.erp_capture_store import find_saved_order, save_capture
    from app.services.erp_pricing import preview_pricing

    result, _ = run(tmp_path, issue='duplicate_finance')
    group = result['groups'][0]
    assert not result['capture_complete'] and not result['selection_complete']
    assert group['error_kind'] == 'duplicate_finance_rows'
    assert group['batches'][0]['scope_check']['duplicate_rows'] == 1

    saved = save_capture(tmp_path, result)
    order = find_saved_order(tmp_path, saved['capture_id'], group['rows'][0]['orderid'])
    assert len(order['groups'][0]['rows']) == 2
    assert order['groups'][0]['finance_error_kind'] == 'duplicate_finance_rows'
    assert saved['cleaning_status'] == 'saved'
    assert not preview_pricing(saved)['source_coverage'][0]['finance_complete']


def test_repeated_business_column_cannot_silently_replace_finance_value(tmp_path):
    result, _ = run(tmp_path, issue='duplicate_column')
    group = result['groups'][0]
    assert not result['capture_complete']
    assert '重复业务列' in group['error']
    assert not group['rows']
    assert any('order-finance' in item['file'] for item in group['files'])


@pytest.mark.parametrize('corruption', ['finance', 'loss_pool', 'missing_manifest'])
def test_replay_checks_originals_against_the_saved_projection(tmp_path, corruption):
    from scripts.verify_erp_replay import check_originals

    capture, _ = run(tmp_path)
    verified = check_originals(capture)
    assert not verified['failures']

    group = capture['groups'][0]
    if corruption == 'finance':
        group['rows'][0]['fields']['修正一次毛利'] = '-999'
    elif corruption == 'loss_pool':
        group['loss_rows'][0]['毛利'] = '-999'
    else:
        group['files'].pop()
    failed = check_originals(capture)
    expected = 'missing_original' if corruption == 'missing_manifest' else f'{corruption}_projection_mismatch'
    assert any(item['reason'] == expected for item in failed['failures'])
    assert verified['projected_rows'] == {'loss_pool': 1, 'finance': 1}


def test_unknown_source_rejected(tmp_path):
    with pytest.raises(ValueError):
        capture_erp_bulk(Settings(_env_file=None,runtime_dir=tmp_path), source='unknown')

def test_cli_defaults_to_bulk_all_sources(monkeypatch, tmp_path, capsys):
    import app.capture_erp as cli
    from app.services.erp_capture_store import load_capture
    calls = []
    def bulk(settings, **kwargs):
        calls.append(kwargs)
        return {'acquisition_mode':'bulk_download', 'groups':[{'source':'loss_allocatable','kind':'loss','status':4,'rows':[], 'error':None}], 'details':[],
                'queried_at':'2026-11-06', 'capture_complete':True,'pricing_ready':False,'summary':{}}
    monkeypatch.setattr(cli,'capture_erp_bulk',bulk,raising=False)
    monkeypatch.setattr(cli,'get_settings',lambda: Settings(_env_file=None,runtime_dir=tmp_path))
    monkeypatch.setattr('sys.argv',['capture_erp'])
    cli.main()
    assert len(calls) == 1 and calls[0]['source'] is None
    assert callable(calls[0]['event_callback'])
    path = next((tmp_path/'source-captures').glob('erp-capture-*.json'))
    assert load_capture(tmp_path,path.stem)['groups'][0]['kind'] == 'loss'


def test_api_defaults_to_bulk_without_pages(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    import app.api.erp_rates as routes
    calls = []
    def bulk(settings, **kwargs):
        calls.append(kwargs)
        return {'acquisition_mode':'bulk_download', 'groups':[],'details':[],'queried_at':'2026-11-06', 'capture_complete':True,
                'pricing_ready':False,'summary':{}}
    monkeypatch.setattr(routes,'capture_erp_bulk',bulk,raising=False)
    settings = Settings(_env_file=None,runtime_dir=tmp_path,database_url=f'sqlite:///{tmp_path / "bulk.db"}',
                        admin_password='test-password',session_secret='s'*40)
    with TestClient(create_app(settings)) as client:
        client.post('/api/auth/login',json={'username':'admin','password':'test-password'})
        r=client.post('/api/sources/erp/captures')
        assert r.status_code == 200
        assert len(calls) == 1 and calls[0]['source'] is None
        assert callable(calls[0]['event_callback'])
        assert client.post('/api/sources/erp/captures?source=unknown').status_code == 422
        # A retired selector must never be ignored and trigger a full ERP download.
        for params in ({'acquisition_mode': 'legacy_pages'}, {'max_pages': 1}, {'page_size': 2}):
            assert client.post('/api/sources/erp/captures', params=params).status_code == 422
        assert len(calls) == 1

def test_truncated_download_becomes_saved_failure(tmp_path):
    def handler(r):
        if r.url.path.endswith('/login'):
            return httpx.Response(200,json={'code':200,'data':{'token':'x'}})
        return httpx.Response(200,content=b'PKtruncated')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_erp_bulk(Settings(_env_file=None,runtime_dir=tmp_path,erp_username='u',erp_password='p'),client=client,source="loss_allocatable")
    assert not result['capture_complete'] and result['groups'][0]['error']
    assert len(list((tmp_path/'source-captures').rglob('result.json'))) == 1


@pytest.mark.parametrize('first_batch_duplicate', [False, True])
def test_bounded_batches_cover_exact_pool_once(tmp_path, first_batch_duplicate):
    ids=['0001','0002','0003']; submissions=[]
    headers=['订单编号','订单状态','平台','集团','毛利','修正毛利','修正一次毛利']
    def rows(selected): return [[i,'可配货','Tiktok','集团二部',-1,-2,-3] for i in selected]
    def handler(r):
        p=r.url.path
        if p.endswith('/login'): return httpx.Response(200,json={'code':200,'data':{'token':'x'}})
        if p.endswith('/dowDeficitOrder'): return httpx.Response(200,content=xlsx(headers,rows(ids)))
        if p.endswith('/asyncOrderListDownload'):
            submissions.append(json.loads(r.content)['orderIdList'])
            return httpx.Response(200,json={'code':200,'data':f'下载任务【batch{len(submissions)}】后台执行中'})
        if p.endswith('/download/list'):
            n=len(submissions)
            return httpx.Response(200,json={'code':200,'data':[{'source':'orderList','id':str(n),'fileName':f'batch{n}', 'status':'success','downloadUrl':f'/batch{n}.xlsx'}]})
        exported = rows(submissions[-1])
        if first_batch_duplicate and len(submissions) == 1:
            exported.append(exported[0])
        return httpx.Response(200,content=xlsx(headers,exported))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_erp_bulk(Settings(_env_file=None,runtime_dir=tmp_path,erp_username='u',erp_password='p'),client=client,source="loss_allocatable",export_batch_size=2)
    assert result['capture_complete'] is (not first_batch_duplicate)
    assert result['summary']['unique_orders'] == 3
    assert result['groups'][0]['batches'][1]['status'] == 'complete'
    assert submissions == [ids[:2],ids[2:]]
    assert len(result['groups'][0]['batches'])==2

    from scripts.verify_erp_replay import check_originals

    assert check_originals(result)['failures'] == []
    first, second = result['groups'][0]['batches']
    first['order_ids'].append(first['order_ids'][0])
    repeated = check_originals(result)['failures']
    assert [(item['batch'], item['reason']) for item in repeated] == [(1, 'finance_batch_scope_mismatch')]
    first['order_ids'].pop()
    first['order_ids'][0], second['order_ids'][0] = second['order_ids'][0], first['order_ids'][0]
    mismatches = check_originals(result)['failures']
    assert {item['batch'] for item in mismatches if item['reason'] == 'finance_batch_scope_mismatch'} == {1, 2}


def test_cli_partial_capture_has_nonzero_exit_after_save(monkeypatch,tmp_path):
    import app.capture_erp as cli
    monkeypatch.setattr(cli,'get_settings',lambda:Settings(_env_file=None,runtime_dir=tmp_path))
    monkeypatch.setattr(cli,'capture_erp_bulk',lambda *a,**kw:{'acquisition_mode':'bulk_download', 'groups':[],'details':[], 'queried_at':'2026-11-06', 'capture_complete':False,'summary':{},'pricing_ready':False})
    monkeypatch.setattr('sys.argv',['capture_erp'])
    with pytest.raises(SystemExit) as e: cli.main()
    assert e.value.code == 1
    assert len(list((tmp_path/'source-captures').glob('erp-capture-*.json')))==1

def test_rate_limit_keeps_completed_batch_and_unattempted_orders(tmp_path):
    ids=['0001','0002','0003','0004','0005']; submissions=[]
    headers=['订单编号','订单状态','平台','集团','毛利','修正毛利','修正一次毛利']
    def rows(selected):return [[i,'可配货','Tiktok','集团二部',-1,-2,-3] for i in selected]
    def handler(r):
        p=r.url.path
        if p.endswith('/login'):return httpx.Response(200,json={'code':200,'data':{'token':'x'}})
        if p.endswith('/dowDeficitOrder'):return httpx.Response(200,content=xlsx(headers,rows(ids)))
        if p.endswith('/asyncOrderListDownload'):
            submissions.append(json.loads(r.content)['orderIdList'])
            return httpx.Response(200,json={'code':200,'data':'下载任务【first】后台执行中'} if len(submissions)==1 else {'code':500003})
        if p.endswith('/download/list'):
            return httpx.Response(200,json={'code':200,'data':[{'source':'orderList','id':'1','fileName':'first','status':'success','downloadUrl':'/first.xlsx'}]})
        return httpx.Response(200,content=xlsx(headers,rows(ids[:2])))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_erp_bulk(Settings(_env_file=None,runtime_dir=tmp_path,erp_username='u',erp_password='p'),client=client,source="loss_allocatable",export_batch_size=2)
    assert result['stopped_by_rate_limit'] and not result['capture_complete']
    assert result['summary']['unique_orders']==2
    assert [b['status'] for b in result['groups'][0]['batches']]==['complete','failed','not_attempted']
    assert len(submissions)==2

    from app.services.erp_capture_store import find_saved_order, save_capture
    capture_id = save_capture(tmp_path, result)["capture_id"]
    failed = find_saved_order(tmp_path, capture_id, "0003")["groups"][0]
    skipped = find_saved_order(tmp_path, capture_id, "0005")["groups"][0]
    assert failed["finance_error_kind"] == "finance_export_failed"
    assert skipped["finance_error_kind"] == "finance_export_not_attempted"
    assert failed["finance_batches"][0]["status"] == "failed"
    assert skipped["finance_batches"][0]["status"] == "not_attempted"

def test_export_preserves_stale_loss_status_as_evidence(tmp_path):
    from app.adapters.erp_bulk_capture import _scope
    # 亏损池状态为可配货，但完整订单表可能已完成；只记差异，不丢弃来源记录。
    _scope([{'订单编号':'A','平台':'Tiktok','集团':'集团二部','订单状态':'已完成'}], '集团二部')


def test_status_difference_is_retained_without_failing_batch(tmp_path):
    result,calls=run(tmp_path,issue='status')
    assert result['capture_complete']
    assert result['summary']['status_discrepancies']==1
    assert result['groups'][0]['rows'][0]['current_status_matches_source'] is False
    assert result['groups'][0]['rows'][0]['fields']['订单状态']=='已完成'


def test_null_status_stays_null_and_summary_is_json_safe(tmp_path):
    result,calls=run(tmp_path,issue='null_status')
    assert result['capture_complete']
    assert result['groups'][0]['rows'][0]['fields']['订单状态'] is None
    assert result['summary']['order_status_counts']=={'未知/空值':1}

def test_missing_orders_do_not_discard_available_orders_or_later_batches(tmp_path):
    ids=['0001','0002','0003'];subs=[]
    headers=['订单编号','订单状态','平台','集团','毛利','修正毛利','修正一次毛利']
    def rows(selected):return [[i,'可配货','Tiktok','集团二部',-1,-2,-3] for i in selected]
    def handler(r):
        p=r.url.path
        if p.endswith('/login'):return httpx.Response(200,json={'code':200,'data':{'token':'x'}})
        if p.endswith('/dowDeficitOrder'):return httpx.Response(200,content=xlsx(headers,rows(ids)))
        if p.endswith('/asyncOrderListDownload'):
            subs.append(json.loads(r.content)['orderIdList']);return httpx.Response(200,json={'code':200,'data':f'下载任务【b{len(subs)}】后台执行中'})
        if p.endswith('/download/list'):
            n=len(subs);return httpx.Response(200,json={'code':200,'data':[{'source':'orderList','id':str(n),'fileName':f'b{n}','status':'success','downloadUrl':'/export.xlsx'}]})
        return httpx.Response(200,content=xlsx(headers,rows(['0001'] if len(subs)==1 else ['0003'])))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_erp_bulk(Settings(_env_file=None,runtime_dir=tmp_path,erp_username='u',erp_password='p'),client=client,source="loss_allocatable",export_batch_size=2)
    assert not result['capture_complete']
    assert [b['status'] for b in result['groups'][0]['batches']]==['partial','complete']
    assert result['groups'][0]['missing_order_ids']==['0002']
    assert {r['orderid'] for r in result['groups'][0]['rows']}=={'0001','0003'}
    assert result['groups'][0]['error']

    from app.services.erp_capture_store import find_saved_order, save_capture

    capture_id = save_capture(tmp_path, result)["capture_id"]
    missing = find_saved_order(tmp_path, capture_id, "0002")["groups"][0]
    assert missing["source"] == "loss_allocatable"
    assert missing["finance_error_kind"] == "not_returned_by_bulk_export"
    batch, = missing["finance_batches"]
    assert batch["number"] == 1 and batch["status"] == "partial"
    original, = batch["originals"]
    assert Path(original["file"]).name == "loss_allocatable-order-finance-001.xlsx"
    assert len(original["sha256"]) == 64
    returned = find_saved_order(tmp_path, capture_id, "0003")["groups"][0]
    assert [batch["number"] for batch in returned["finance_batches"]] == [2]


def test_lookup_missing_finance_order_returns_loss_source_and_explicit_error():
    from app.services.erp_capture_store import find_order
    data={'capture_id': 'sample-capture', 'queried_at':'2026-11-06','groups':[{'kind':'loss','status':4,'rows':[],
          'loss_rows':[{'订单编号':'OLD','毛利':None}], 'missing_order_ids':['OLD']}],'details':[]}
    order=find_order(data,'OLD')
    assert order['groups'][0]['loss_rows'][0]['订单编号']=='OLD'
    assert order['groups'][0]['finance_error_kind']=='not_returned_by_bulk_export'
    assert order['detail'] is None


def test_bulk_sample_is_explicit_and_progress_is_safe(tmp_path):
    events = []
    result, calls = run(tmp_path, max_orders=1, progress=events.append)
    assert result['sample_only'] is True
    assert result['selection_complete'] is True
    assert result['capture_complete'] is False
    assert result['summary']['selected_orders'] == 1
    assert result['summary']['orders_not_selected'] == 0
    assert events and 'SECRET' not in str(events)
    assert result['source_kind'] == 'erp_web_backend'


def test_sample_limits_export_without_discarding_source_pool(tmp_path):
    ids = ['A','B','C']; submissions = []
    headers = ['订单编号','订单状态','平台','集团','毛利','修正毛利','修正一次毛利']
    def rows(selected): return [[i,'可配货','Tiktok','集团二部',-1,-2,-3] for i in selected]
    def handler(r):
        path = r.url.path
        if path.endswith('/login'): return httpx.Response(200,json={'code':200,'data':{'token':'secret'}})
        if path.endswith('/dowDeficitOrder'): return httpx.Response(200,content=xlsx(headers,rows(ids)))
        if path.endswith('/asyncOrderListDownload'):
            submissions.append(json.loads(r.content)['orderIdList'])
            return httpx.Response(200,json={'code':200,'data':'下载任务【sample】后台执行中'})
        if path.endswith('/download/list'):
            return httpx.Response(200,json={'code':200,'data':[{'source':'orderList','id':'1','fileName':'sample','status':'success','downloadUrl':'/sample.xlsx'}]})
        return httpx.Response(200,content=xlsx(headers,rows(ids[:2])))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_erp_bulk(Settings(_env_file=None,runtime_dir=tmp_path,erp_username='u',erp_password='p'),client=client,source="loss_allocatable",max_orders=2)
    assert submissions == [['A','B']]
    assert len(result['groups'][0]['loss_rows']) == 3
    assert result['summary']['orders_not_selected'] == 1
    assert result['selection_complete'] and not result['capture_complete']
    assert result['summary']['missing_orders'] == 0

    from app.services.erp_capture_store import find_saved_order, save_capture
    capture_id = save_capture(tmp_path, result)["capture_id"]
    omitted = find_saved_order(tmp_path, capture_id, "C")["groups"][0]
    assert omitted["finance_error_kind"] == "finance_not_selected"
    assert "finance_batches" not in omitted


@pytest.mark.parametrize('limit',[0,-1,True,1.5])
def test_invalid_sample_limit_rejected_before_network(tmp_path,limit):
    with pytest.raises(ValueError):
        capture_erp_bulk(Settings(_env_file=None,runtime_dir=tmp_path),max_orders=limit)


@pytest.mark.parametrize('issue',[None,'unknown','ambiguous','wrong_owner','missing_owner'])
def test_salesperson_filter_is_resolved_and_applied_at_source(tmp_path,issue):
    calls=[]
    headers=['订单编号','订单状态','平台','集团','销售员','毛利','修正毛利','修正一次毛利']
    owner='其他人' if issue=='wrong_owner' else None if issue=='missing_owner' else '杨颖'
    def handler(r):
        path=r.url.path;body=json.loads(r.content) if r.content else {};calls.append((path,body))
        if path.endswith('/login'):return httpx.Response(200,json={'code':200,'data':{'token':'secret'}})
        if path.endswith('/selectSellman'):
            people=[] if issue=='unknown' else [{'userName':'杨颖','userID':'Y'}]
            if issue=='ambiguous': people.append({'userName':'杨颖','userID':'Y2'})
            return httpx.Response(200,json={'code':200,'data':people})
        if path.endswith('/dowDeficitOrder'):
            assert body=={'platform':'Tiktok','bloc':'集团二部','status':4,'username':'杨颖','financecheck':'3'}
            return httpx.Response(200,content=xlsx(headers,[['A','可配货','Tiktok','集团二部',owner,-1,-2,-3]]))
        if path.endswith('/asyncOrderListDownload'):
            assert body=={'orderIdList':['A']}
            return httpx.Response(200,json={'code':200,'data':'下载任务【杨颖本次】后台执行中'})
        if path.endswith('/download/list'):
            return httpx.Response(200,json={'code':200,'data':[{'id':'t','source':'orderList','fileName':'杨颖本次','status':'success','downloadUrl':'/y.xlsx'}]})
        return httpx.Response(200,content=xlsx(headers,[['A','可配货','Tiktok','集团二部','杨颖',-1,-2,-3]]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=capture_erp_bulk(Settings(_env_file=None,runtime_dir=tmp_path,erp_username='本人',erp_password='p'),client=client,source='loss_allocatable',salesperson='杨颖')
    assert result['capture_complete'] is (issue is None)
    assert result['groups'][0]['filters']['username']=='杨颖'
    if issue is None:
        assert result['groups'][0]['rows'][0]['fields']['销售员']=='杨颖'
        assert result['salesperson']=={'name':'杨颖','id':'Y'}
    else:
        assert result['groups'][0]['error']
        assert not any(path.endswith('/asyncOrderListDownload') for path,body in calls)


def test_review_columns_follow_manual_export_and_keep_raw_values(tmp_path):
    from app.adapters.erp_bulk_capture import write_comparison_workbook, MANUAL_ORDER_HEADERS
    from openpyxl import load_workbook
    oid = '0001234567890123456789'
    loss = xlsx(['订单编号','财务复核','自动放行时间'], [[oid,None,None]])
    finance = xlsx(['新增费用','修正一次毛利','订单编号','订单状态'],
                   [[0.1234,-2.3456,oid,'可配货']])
    pool = tmp_path/'loss.xlsx'; pool.write_bytes(loss)
    source = tmp_path/'finance.xlsx'; source.write_bytes(finance)
    result = write_comparison_workbook(tmp_path,[source],pool)
    w = load_workbook(result['file'],read_only=True,data_only=True)
    rows = list(w.active.values); w.close()
    assert rows[0][:82] == MANUAL_ORDER_HEADERS
    assert rows[0][82:] == ('新增费用','亏损池·财务复核','亏损池·自动放行时间')
    assert rows[1][0] == oid
    assert rows[1][rows[0].index('修正一次毛利')] == -2.3456
    assert rows[1][82] == 0.1234
    assert rows[1][rows[0].index('平台交易费')] is None
    assert '平台交易费' in result['missing_reference_columns']
    assert pool.read_bytes() == loss and source.read_bytes() == finance
    assert result['rows'] == 1

def test_default_bulk_uses_four_verified_candidate_sources(tmp_path):
    calls = []
    headers = ['订单编号','订单状态','平台','集团','毛利','修正毛利','修正一次毛利']
    current = []
    def handler(request):
        path = request.url.path
        if path == '/finance.xlsx':
            return httpx.Response(200, content=xlsx(headers, current))
        body = json.loads(request.content)
        if path.endswith('/login'):
            return httpx.Response(200, json={'code':200,'data':{'token':'SECRET'}})
        if path.endswith('/dowDeficitOrder') or path == '/Api/order/list':
            status = body['status']
            loss = path.endswith('/dowDeficitOrder')
            order = ('loss' if loss else 'blocked') + str(status)
            label = '可配货' if status == 4 else '待处理'
            current[:] = [[order,label,'Tiktok','集团二部',-1,-1,-1]]
            calls.append((path, body))
            if loss:
                return httpx.Response(200, content=xlsx(headers, current))
            row = {'orderid':order,'status':status,'jzfh':'1','platform':'Tiktok'}
            return httpx.Response(200, json={'code':0,'data':{'records':[row],'total':1}})
        if path.endswith('/asyncOrderListDownload'):
            assert body == {'orderIdList':[current[0][0]]}
            return httpx.Response(200, json={'code':200,'data':'下载任务【task】后台执行中'})
        if path.endswith('/download/list'):
            return httpx.Response(200, json={'code':200,'data':[
                {'id':'id','fileName':'task','source':'orderList','status':'success','downloadUrl':'/finance.xlsx'}]})
        pytest.fail(path)
    settings = Settings(_env_file=None,runtime_dir=tmp_path,erp_username='u',erp_password='p')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_erp_bulk(settings, client=client)
    assert result['capture_complete']
    assert [(g['kind'],g['status'],g['rows'][0]['orderid']) for g in result['groups']] == [
        ('loss',4,'loss4'), ('loss',3,'loss3'), ('blocked',4,'blocked4'), ('blocked',3,'blocked3')]
    for path, body in calls[:2]:
        assert path.endswith('/dowDeficitOrder') and body['financecheck'] == '3'
    for path, body in calls[2:]:
        assert path == '/Api/order/list' and body['jzfh'] == '1'
        assert 'financecheck' not in body and body['limit'] == 20
    for group in result['groups']:
        assert group['list_complete']
        assert group['status_discrepancies'] == []
        assert group['batches'][0]['scope_check']['missing'] == []
        assert group['batches'][0]['scope_check']['unexpected'] == []


def test_blocked_small_pages_have_disjoint_verified_ranges():
    from app.adapters.erp_bulk_capture import _blocked_ids
    rows = [{'orderid': f'B{i}', 'status': 4, 'platform': 'Tiktok', 'jzfh': '1'} for i in range(21)]
    def post(path, body):
        start = (body['page'] - 1) * body['limit']
        return {'total': 21, 'records': rows[start:start+body['limit']]}
    group = {'status':4, 'filters':{}, 'pages':[], 'total':None}
    orders = _blocked_ids(post, group)
    assert len(orders) == 21
    assert [(p['start'],p['end']) for p in group['pages'] if p['verified']] == [(0,20),(20,21)]


@pytest.mark.parametrize('drift', [False, True])
def test_blocked_pagination_uses_observed_orders_despite_total_drift(tmp_path, drift):
    pages = []
    submissions = []
    headers = ['订单编号','订单状态','平台','集团','毛利','修正毛利','修正一次毛利']
    def handler(request):
        path = request.url.path
        if path == '/finance.xlsx':
            return httpx.Response(200, content=xlsx(headers, [
                [f'B{i:04d}','待处理','Tiktok','集团二部',-1,-1,-1] for i in range(21)]))
        body = json.loads(request.content)
        if path.endswith('/login'):
            return httpx.Response(200, json={'code':200,'data':{'token':'SECRET'}})
        if path == '/Api/order/list':
            pages.append(body['page'])
            assert body['jzfh'] == '1' and 'financecheck' not in body
            start, stop = (0,20) if body['page'] == 1 else (20,21)
            records = [{'orderid':f'B{i:04d}','status':3,'platform':'Tiktok','jzfh':'1'}
                       for i in range(start,stop)]
            total = 22 if drift and body['page'] == 2 else 21
            return httpx.Response(200, json={'code':0,'data':{'total':total,'records':records}})
        if path.endswith('/asyncOrderListDownload'):
            submissions.append(body)
            return httpx.Response(200, json={'code':200,'data':'下载任务【task】后台执行中'})
        if path.endswith('/download/list'):
            return httpx.Response(200, json={'code':200,'data':[
                {'id':'id','fileName':'task','source':'orderList','status':'success','downloadUrl':'/finance.xlsx'}]})
        pytest.fail(path)
    settings = Settings(_env_file=None,runtime_dir=tmp_path,erp_username='u',erp_password='p')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_erp_bulk(settings, source='blocked_pending', client=client)
    group = result['groups'][0]
    assert pages == [1,2]
    assert group['list_complete']
    assert submissions == [{'orderIdList':[f'B{i:04d}' for i in range(21)]}]
    assert group['batches'][0]['status'] == 'complete'
    assert len(group['candidate_order_ids']) == group['total'] == 21
    assert group['failed_pages'] == [] and 'list_error' not in group
    assert group['error'] is None
    assert group['pages'][-1]['total'] == (22 if drift else 21)
    assert result['selection_complete'] and result['capture_complete']
    from app.adapters.erp_pricing import CaptureGroup
    normalized = CaptureGroup.model_validate(group)
    assert normalized.finance_coverage('集团二部') == (True, False)


@pytest.mark.parametrize('totals, counts', [
    ([286, 289], [20, 3]),
    ([40, 20], [20, 0]),
    ([20], [3]),
    ([0], [0]),
    ([21, 40], [20, 20]),
])
def test_blocked_dynamic_counts_end_without_incomplete_or_rescan(totals, counts):
    from app.adapters.erp_bulk_capture import _blocked_ids
    requested = []
    def post(path, body):
        page = body['page']
        requested.append(page)
        start = (page - 1) * 20
        return {'total': totals[page - 1], 'records': [
            {'orderid':f'B{i}', 'status':4, 'platform':'Tiktok', 'jzfh':'1'}
            for i in range(start, start + counts[page - 1])
        ]}
    group = {'status':4, 'filters':{}, 'pages':[], 'total':None}
    ids = _blocked_ids(post, group)
    assert requested == list(range(1, len(counts) + 1))
    assert len(ids) == group['total'] == sum(counts)
    assert 'list_error' not in group and not group.get('failed_pages')
