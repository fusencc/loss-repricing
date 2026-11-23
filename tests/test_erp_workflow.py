"""单进程编排验收；外部 ERP 仅在 HTTP 边界模拟。"""
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal

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


@pytest.mark.parametrize('fx_available', [True, False])
def test_fresh_collection_runs_download_clean_evaluate_and_archive(tmp_path, monkeypatch, fx_available):
    from app.services import erp_workflow
    from test_erp_bulk_capture import run
    from datetime import datetime, timedelta, timezone
    from app.domain.engine import BEIJING
    collected = {}

    # 替代外部 HTTP 服务；实际采集器、XLSX读取、归档、SQLite及规则均执行。
    def collect(settings, **kwargs):
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
        observed = max(datetime.now(timezone.utc), max(datetime.fromisoformat(t['queried_at']) for group in collected['groups']
                       for batch in group['batches'] for t in batch['polls'])) + timedelta(seconds=1)
        return {'date':observed.astimezone(BEIJING).date().isoformat(), 'display_units_per_cny':{'CNY':'1','PHP':'10'},
                'version':'erp-current-config-usage-day-v1', 'ref':'test-company-fx',
                'queried_at':observed.isoformat(), 'synthetic':True,
                'source_effective_date':None, 'date_policy':'current_config_on_usage_day',
                'source_rates':{'CNY':'1','PHP':'0.1'}}
    monkeypatch.setattr(erp_workflow, 'query_current_erp_fx', company_fx, raising=False)
    monkeypatch.setattr(erp_workflow, 'capture_recent_price_changes',
        lambda *args,**kwargs:pytest.fail('没有可算价目标时不应查询调价日志'))
    workflow = erp_workflow.run_workflow(Settings(_env_file=None, runtime_dir=tmp_path))
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


def test_cli_missing_evidence_returns_nonzero_after_persisting_report(tmp_path):
    _, capture_id, _ = inputs(tmp_path)
    command = subprocess.run(
        [sys.executable, '-m', 'app.erp_workflow', '--capture-id', capture_id],
        env=dict(os.environ, PYTHONPATH='backend', LR_RUNTIME_DIR=str(tmp_path)),
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
