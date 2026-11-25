"""单进程编排验收；外部 ERP 仅在 HTTP 边界模拟。"""
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal
from threading import Event, get_ident

import pytest

from app.config import Settings
from app.services.erp_capture_store import save_capture


def inputs(tmp_path):
    fixtures = Path(__file__).parent / 'fixtures'
    capture = json.loads((fixtures / 'erp-pricing-capture.sample.json').read_text())
    evidence = json.loads((fixtures / 'erp-pricing-evidence.sample.json').read_text())
    saved = save_capture(tmp_path, capture)
    evidence['capture_id'] = saved['capture_id']
    config = Settings(_env_file=None, runtime_dir=tmp_path)
    return config, saved['capture_id'], evidence


def test_workflow_archives_only_ready_prices_and_preserves_unresolved_orders(tmp_path):
    from app.services.erp_workflow import run_workflow

    config, capture_id, evidence = inputs(tmp_path)
    result = run_workflow(config, capture_id=capture_id, evidence=evidence)
    root = Path(result['artifact_dir'])
    pending = json.loads((root / 'pending-prices.json').read_text())
    assert result['status'] == 'needs_attention'  # 第三笔订单缺明细亏损，不能宣称全部闭环。
    assert result['ready_prices'] == 2
    assert len(pending['decisions']) == 2
    price = pending['decisions'][0]
    assert price['key']['item_id'] == '0001001'
    assert price['upload_price'] == '20.80'
    assert Decimal(price['target_actual']) == Decimal('18.72')
    assert json.loads(price['selected_row_id']) == ['000001', '01']
    unresolved = json.loads((root / 'unresolved.json').read_text())
    assert any(row['code'] == 'missing_line_loss' for row in unresolved['issues'])
    assert not any(row['reason_code'] == 'sales_protection' for row in unresolved['decisions'])
    assert pending['capture_id'] == capture_id and pending['synthetic'] is True
    assert not pending['execution_allowed']
    assert json.loads((root / 'result.json').read_text()) == result


def test_complete_evidence_finishes_without_reclassifying_protected_target(tmp_path):
    from app.services.erp_workflow import run_workflow

    config, capture_id, evidence = inputs(tmp_path)
    evidence['orders'][2]['lines'][0]['loss'] = dict(evidence['orders'][0]['lines'][0]['loss'])
    mapping = evidence['orders'][2]['lines'][0]['mapping']
    online = deepcopy(evidence['online'][0])
    online.update(ref='sample-online-state/0001003')
    online['state'].update(key=mapping['key'], main_sku=mapping['main_sku'])
    evidence['online'].append(online)
    result = run_workflow(config, capture_id=capture_id, evidence=evidence)
    assert result['status'] == 'completed'
    assert result['ready_prices'] >= 1


def test_missing_evidence_still_saves_gap_archive_without_success(tmp_path):
    from app.services.erp_workflow import run_workflow

    config, capture_id, _ = inputs(tmp_path)
    result = run_workflow(config, capture_id=capture_id)
    assert result['status'] == 'blocked'
    assert result['ready_prices'] == 0
    root = Path(result['artifact_dir'])
    assert json.loads((root / 'pending-prices.json').read_text())['decisions'] == []
    assert len(json.loads((root / 'unresolved.json').read_text())['issues']) == 3


def test_failed_cleaning_does_not_publish_pending_prices(tmp_path, monkeypatch):
    from app.services import erp_workflow

    config, capture_id, _ = inputs(tmp_path)
    def fail(*args):
        raise sqlite3.OperationalError('test cleaning failure')
    monkeypatch.setattr(erp_workflow, 'rebuild_clean_capture', fail)
    with pytest.raises(sqlite3.OperationalError):
        erp_workflow.run_workflow(config, capture_id=capture_id)
    assert not list(tmp_path.rglob('pending-prices.json'))


def test_roster_overlaps_erp_with_sales_and_isolates_scope_failures(tmp_path, monkeypatch):
    from app.services import erp_workflow as workflow

    names = ['甲', '乙', '丙', '丁']
    department = 'Tiktok一部'
    monkeypatch.setattr(workflow, '_roster', lambda *_: names)
    monkeypatch.setattr(workflow, 'ERP_ROSTER_DEPARTMENTS', dict.fromkeys(names, department))
    second_started, first_in_sales = Event(), Event()
    erp_calls, sales_calls, public_calls, callbacks = [], [], [], []
    fees = object()
    fx = {'ref': 'test-shared-fx'}

    def refresh(settings, *, client, auth):
        public_calls.append(('fees', get_ident(), client, auth))
        return fees

    def query(settings):
        public_calls.append(('fx', get_ident()))
        return fx

    def bulk(settings, *, salesperson, web_session, **kwargs):
        erp_calls.append(('bulk', salesperson, get_ident(), web_session))
        if salesperson == '乙':
            second_started.set()
            assert first_in_sales.wait(5), 'ERP 不应在首人销售启动前就结束第二人'
        return {'capture_id': salesperson, 'capture_complete': False, 'cleaning_status': 'saved'}

    def save(runtime, capture):
        erp_calls.append(('save', capture['capture_id']))
        return capture

    def headers(settings, capture, *, web_session, **kwargs):
        erp_calls.append(('headers', capture['capture_id'], get_ident(), web_session))
        return {'name': capture['capture_id']}

    def details(settings, capture, *, web_session, **kwargs):
        erp_calls.append(('details', capture['capture_id'], get_ident(), web_session))
        if capture['capture_id'] == '乙':
            raise RuntimeError('mock ERP detail failure')
        return {'name': capture['capture_id']}

    def sales(settings, capture, *, headers, native, daily_fees, current_fx, client, auth, **kwargs):
        name = capture['capture_id']
        sales_calls.append((name, get_ident(), client, auth))
        if name == '甲':
            first_in_sales.set()
            assert second_started.wait(5), '第二人的 ERP 应与首人销售重叠'
        assert headers == native == {'name': name}
        assert daily_fees is fees and current_fx is fx
        return {'status': 'completed', 'ready_prices': 1}

    def submit(scope):
        callbacks.append((scope['salesperson'], get_ident()))
        if scope['salesperson'] == '丙':
            raise RuntimeError('mock submission failure')
        scope['callback_completed'] = True

    for name, fake in [('refresh_daily_fees', refresh), ('query_current_erp_fx', query),
                       ('capture_erp_bulk', bulk), ('save_capture', save), ('capture_order_headers', headers),
                       ('capture_erp_details', details), ('_evaluate_capture', sales)]:
        monkeypatch.setattr(workflow, name, fake)
    result = workflow.run_workflow(Settings(_env_file=None, runtime_dir=tmp_path), on_roster_scope=submit)

    assert [call[0] for call in public_calls] == ['fees', 'fx']
    assert [call[:2] for call in erp_calls] == [(stage, name) for name in names
                                               for stage in ('bulk', 'save', 'headers', 'details')]
    erp_thread, erp_session = erp_calls[0][2:]
    assert all(call[2:] == (erp_thread, erp_session) for call in erp_calls if call[0] != 'save')
    _, sales_thread, client, auth = public_calls[0]
    assert public_calls[1] == ('fx', sales_thread)
    assert erp_thread != sales_thread != get_ident()
    assert sales_calls == [(name, sales_thread, client, auth) for name in ('甲', '丙', '丁')]
    assert callbacks == [(name, sales_thread) for name in names]
    assert [scope['status'] for scope in result['departments']] == ['completed', 'failed', 'failed', 'completed']
    assert result['departments'][1]['capture_id'] == '乙'
    assert result['departments'][-1]['callback_completed']
    assert result['status'] == 'needs_attention' and result['ready_prices'] == 2
    assert json.loads(Path(result['artifact_dir'], 'result.json').read_text()) == result


@pytest.mark.parametrize('fx_available', [True, False])
def test_fresh_collection_runs_download_clean_evaluate_and_archive(tmp_path, monkeypatch, fx_available):
    from app.services import erp_workflow
    from test_erp_bulk_capture import run
    from test_daily_sales_fees import setup as setup_fees
    from datetime import datetime, timezone
    from app.domain.engine import BEIJING
    from app.adapters.current_pricing_fees import refresh_daily_fees
    collected = {}
    _, fee_rows, _, fee_client = setup_fees(tmp_path)
    fee_rows['_invalid'] = list(fee_rows)
    connections = {}
    def refresh(settings, **kwargs):
        connections['fees'] = (kwargs['client'], kwargs['auth'])
        return refresh_daily_fees(settings, client=fee_client, synthetic=True)
    monkeypatch.setattr(erp_workflow, 'refresh_daily_fees', refresh)

    def observe_stage(name, keys):
        capture_stage = getattr(erp_workflow, name)
        def observed(*args, **kwargs):
            connections[name] = tuple(kwargs[key] for key in keys)
            return capture_stage(*args, **kwargs)
        monkeypatch.setattr(erp_workflow, name, observed)
    observe_stage('capture_order_headers', ('web_session',))
    observe_stage('capture_erp_details', ('web_session',))
    observe_stage('capture_sales_batch', ('client', 'auth'))

    # 替代外部 HTTP 服务；实际采集器、XLSX读取、归档、SQLite及规则均执行。
    def collect(settings, **kwargs):
        connections['bulk'] = (kwargs['web_session'],)
        capture, _ = run(settings.runtime_dir)
        capture['synthetic'] = True
        capture['department'] = kwargs['department']
        collected.update(capture)
        return capture
    monkeypatch.setattr(erp_workflow, 'capture_erp_bulk', collect)
    monkeypatch.setattr(erp_workflow, 'ERP_DEPARTMENTS', ('Tiktok一部',))
    def company_fx(settings):
        if not fx_available:
            from app.adapters.base import SourceUnavailable
            raise SourceUnavailable('test company FX unavailable')
        observed = datetime.now(timezone.utc)
        return {'date':observed.astimezone(BEIJING).date().isoformat(), 'display_units_per_cny':{'CNY':'1','PHP':'10'},
                'version':'erp-current-config-usage-day-v1', 'ref':'test-company-fx',
                'queried_at':observed.isoformat(), 'synthetic':True,
                'source_effective_date':None, 'date_policy':'current_config_on_usage_day',
                'source_rates':{'CNY':'1','PHP':'0.1'}}
    monkeypatch.setattr(erp_workflow, 'query_current_erp_fx', company_fx, raising=False)
    monkeypatch.setattr(erp_workflow, 'capture_recent_price_changes',
        lambda *args,**kwargs:pytest.fail('没有可算价目标时不应查询调价日志'))
    # 这个用例覆盖普通部门采集，机器当天恰逢固定名单日也使用同一测试范围。
    workflow = erp_workflow.run_workflow(Settings(_env_file=None, runtime_dir=tmp_path, erp_roster_date=None))
    assert connections['bulk'][0] is not None
    assert connections['bulk'] == connections['capture_order_headers'] == connections['capture_erp_details']
    assert connections['fees'] == connections['capture_sales_batch']
    result, = workflow['departments']
    assert result['mode'] == 'capture' and result['status'] == 'blocked'
    assert result['summary']['unique_orders'] == 1
    assert result['cleaning_status'] == 'saved'
    assert len(list((tmp_path / 'source-captures').rglob('*.xlsx'))) == 3
    with sqlite3.connect(tmp_path / 'erp-clean.sqlite3') as db:
        assert db.execute('select count(*) from observations').fetchone()[0] == 1
    assert (Path(result['artifact_dir']) / 'pending-prices.json').is_file()
    candidates = json.loads(Path(result['seller_candidates_file']).read_text())
    assert candidates['capture_id'] == result['capture_id']
    assert candidates['department'] == 'Tiktok一部'
    assert candidates['sellers'] == {}
    assert [item['reason'] for item in candidates['unresolved']] == ['shop_unverified']
    assert result['fx_status'] == ('saved' if fx_available else 'unavailable')
    assert (tmp_path / 'source-fx' / 'test-company-fx.json').is_file() is fx_available
    if fx_available:
        stored_fx = json.loads((tmp_path / 'source-fx' / 'test-company-fx.json').read_text())
        assert datetime.fromisoformat(stored_fx['queried_at']) < datetime.fromisoformat(collected['queried_at'])
    assert any('汇率' in message for message in result['global_issues']) is not fx_available
    # ERP财务样例没有父产品字段，销售阶段应留下具体缺项，而非声称已取全。
    assert result['sales_status'] == 'partial'
    report_path = tmp_path / 'erp-pricing-previews' / (result['preview_id'] + '.json')
    sales_ref = json.loads(report_path.read_text())['references']['sales_source']
    assert sales_ref['erp_capture_id'] == result['capture_id']
    assert sales_ref['acquisition_complete'] is False
    sales_file = Path(sales_ref['artifact_file'])
    assert json.loads(sales_file.read_text())['issues'][0]['code'] == 'missing_product_hint'
    assert result['history_scope'] == 'official_type_2_last_72_hours'
    refs = json.loads(report_path.read_text())['references']
    assert 'company_price_history' not in refs and 'company_price_effects' not in refs
    assert refs['fixed_fees']['erp_capture_id'] == result['capture_id']
    assert result['fixed_fee_status'] == 'unavailable'


def test_manual_cli_stops_with_ready_prices_even_if_submit_enabled(tmp_path, monkeypatch, capsys):
    from app import erp_workflow as cli
    from app.adapters.http import HttpAdapter
    from app.adapters.manual_live import ManualLiveAdapter

    config, capture_id, evidence = inputs(tmp_path)
    config.live_submit_enabled = True
    monkeypatch.setattr(cli, 'get_settings', lambda: config)
    def read_only_run(settings, **kwargs):
        assert not settings.live_submit_enabled
        return run_workflow(settings, **kwargs)
    from app.services.erp_workflow import run_workflow
    monkeypatch.setattr(cli, 'run_workflow', read_only_run)
    def unexpected_upload(*args, **kwargs):
        pytest.fail('手动测试不得调用价格上传')
    for adapter in (HttpAdapter, ManualLiveAdapter):
        monkeypatch.setattr(adapter, 'submit', unexpected_upload)
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps(evidence))
    monkeypatch.setattr(sys, 'argv', ['erp_workflow', '--capture-id', capture_id, '--evidence', str(path)])
    assert cli.main() == 2  # 两条可算，另一条缺项，不以就绪数覆盖原退出码。
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert config.live_submit_enabled  # 本次关闭不回写原配置。
    assert '算价通过 2 条，未提交价格' in output.err
    pending = Path(result['artifact_dir']) / 'pending-prices.json'
    assert str(pending) in output.err and len(json.loads(pending.read_text())['decisions']) == 2


def test_cli_missing_evidence_returns_nonzero_after_persisting_report(tmp_path):
    _, capture_id, _ = inputs(tmp_path)
    command = subprocess.run(
        [sys.executable, '-m', 'app.erp_workflow', '--capture-id', capture_id],
        env=dict(os.environ, PYTHONPATH='backend', LR_RUNTIME_DIR=str(tmp_path), LR_DATABASE_URL='', LR_LIVE_HISTORY_DATABASE_URL=''),
        capture_output=True, text=True,
    )
    assert command.returncode == 2, command.stderr
    result = json.loads(command.stdout)
    assert result['status'] == 'blocked'
    assert Path(result['artifact_dir'], 'result.json').is_file()


def test_live_fx_context_removes_only_fx_gap_and_keeps_missing_details(tmp_path):
    from app.services.erp_pricing import save_pricing_preview
    config, capture_id, _ = inputs(tmp_path)
    context = {
        'date':'2026-11-09', 'display_units_per_cny':{'CNY':'1','MYR':'0.5'},
        'version':'erp-current-config-usage-day-v1', 'ref':'company-fx-snapshot',
        'queried_at':'2026-11-09T14:50:00+08:00', 'synthetic':True,
        'source_effective_date':None, 'date_policy':'current_config_on_usage_day',
        'source_rates':{'CNY':'1','MYR':'2'},
    }
    report = save_pricing_preview(config.runtime_dir, capture_id, current_fx=context)
    assert report['summary']['ready_prices'] == 0
    assert report['summary']['preparation_issues'] == 3
    assert not any('汇率' in message for message in report['global_issues'])
    assert report['references']['current_fx']['source_effective_date'] is None
    assert report['references']['current_fx']['date_policy'] == 'current_config_on_usage_day'
    context['date'] = '2026-11-10'
    with pytest.raises(ValueError):
        save_pricing_preview(config.runtime_dir, capture_id, current_fx=context)
    context['date'] = '2026-11-09'
    context['synthetic'] = False
    with pytest.raises(ValueError):
        save_pricing_preview(config.runtime_dir, capture_id, current_fx=context)


def test_sampled_sales_source_cannot_be_attached_as_full_source(tmp_path):
    from app.services.erp_pricing import save_pricing_preview
    config, capture_id, _ = inputs(tmp_path)
    with pytest.raises(ValueError):
        save_pricing_preview(config.runtime_dir, capture_id, sales_source={
            'erp_capture_id':capture_id,'synthetic':True,'sample_only':True})




def test_live_order_preparation_archives_facts_without_inventing_online_state(tmp_path):
    from app.services.erp_pricing import save_pricing_preview
    from hashlib import sha256
    config,capture_id,evidence=inputs(tmp_path)
    order=deepcopy(evidence['orders'][0]);order_id=order['order_id']
    order['lines']=order['lines'][:1];line=order['lines'][0]
    line['detail_id']=order_id;line['detail_ref']='native-source/same-sku-order/'+order_id
    line['loss'].update(quantity='1',amount='-2.40',currency='CNY',basis='order',allocation_reliable=True,fee_scope='erp-corrected-first-margin-v1')
    preparation={'schema_version':'erp-order-loss-preparation-v1','version':'erp-same-sku-order-loss-v1',
       'erp_capture_id':capture_id,'synthetic':True,'sample_only':False,'pricing_ready':False,
       'observed_at':'2026-11-09T14:50:00+08:00','orders':[order],
       'issues':[],'shop_scopes':{},'summary':{'prepared_order_losses':1},
       'references':{'native_source':'native-source','sales_source':'sales-source','currency_contract':'erp-order-finance-rmb-20261111'},
       'proofs':{order_id:{'record_kind':'same_sku_order','order_record_id':order_id,'native_refs':['original-xlsx-r2'],
          'quantity':'1','erp_sku':'SAMPLE-A','finance_refs':[{'source':'loss_allocatable','row_index':0}],
          'currency':'CNY','currency_contract':'erp-order-finance-rmb-20261111'}}}
    fx={'date':'2026-11-09','display_units_per_cny':{'CNY':'1','MYR':'0.5'},
        'source_rates':{'CNY':'1','MYR':'2'},'version':'erp-current-config-usage-day-v1','ref':'FX-SOURCE',
        'queried_at':'2026-11-09T14:51:00+08:00','source_effective_date':None,
        'date_policy':'current_config_on_usage_day','synthetic':True}
    report=save_pricing_preview(config.runtime_dir,capture_id,current_fx=fx,order_preparation=preparation)
    assert report['summary']['normalized_detail_lines']==1
    assert report['summary']['ready_prices']==0
    assert report['live_preparation_version']=='erp-same-sku-order-loss-v1'
    ref=report['references']['order_preparation'];data=Path(ref['artifact_file']).read_bytes()
    assert sha256(data).hexdigest()==ref['sha256'] and json.loads(data)==preparation
    stored=json.loads(Path(config.runtime_dir,'erp-pricing-previews',report['preview_id']+'.json').read_text())
    assert stored['_snapshot']['online']==[]
    assert stored['_snapshot']['lines'][0]['currency']=='CNY'
    with pytest.raises(ValueError):
        save_pricing_preview(config.runtime_dir,capture_id,evidence=evidence,order_preparation=preparation)


@pytest.mark.parametrize('source_name',['local_history_source','platform_source','fixed_fees_source'])
@pytest.mark.parametrize('changed',[{'erp_capture_id':'OTHER'},{'sample_only':True},{'synthetic':False}])
def test_source_refs_cannot_cross_capture_or_environment(tmp_path,source_name,changed):
    from app.services.erp_pricing import save_pricing_preview
    config,capture_id,_=inputs(tmp_path)
    source={'source_id':'SOURCE','erp_capture_id':capture_id,'synthetic':True,'sample_only':False,**changed}
    with pytest.raises(ValueError):
        save_pricing_preview(config.runtime_dir,capture_id,**{source_name:source})
