"""部门价格映射只读本地归档，所有外部系统均不参与。"""
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from app.services.erp_capture_store import save_capture
from app.services.erp_department_pricing import load_department_batch
from app.services.erp_pricing import save_capture_preview


@pytest.fixture
def department_artifact(tmp_path):
    fixtures = Path(__file__).parent / 'fixtures'
    capture = save_capture(tmp_path, json.loads((fixtures / 'erp-pricing-capture.sample.json').read_bytes()))
    evidence = json.loads((fixtures / 'erp-pricing-evidence.sample.json').read_bytes())
    evidence['capture_id'] = capture['capture_id']
    preview, _ = save_capture_preview(tmp_path, capture, evidence)
    preview_path = tmp_path / 'erp-pricing-previews' / (preview['preview_id'] + '.json')
    report = json.loads(preview_path.read_bytes())
    report.update(department='Tiktok一部', synthetic=False)
    preview_path.write_text(json.dumps(report), encoding='utf-8')
    directory = tmp_path / 'erp-workflows' / 'department-input'
    directory.mkdir(parents=True)
    seller_file = directory / 'seller-candidates.json'
    shop = report['decisions'][0]['key']['shop_id']
    seller_file.write_text(json.dumps({'sellers': {
        '甲': [{'shop_id': shop, 'order_id': '000001'}],
        '乙': [{'shop_id': shop, 'order_id': '000002'}],
        '同订单号其他店铺': [{'shop_id': 'other-shop', 'order_id': '000002'}],
    }}), encoding='utf-8')
    scope = {'mode': 'capture', 'department': report['department'], 'preview_id': preview['preview_id'],
             'capture_id': capture['capture_id'], 'seller_candidates_file': str(seller_file),
             'artifact_dir': str(directory)}
    return preview_path, report, scope, date.fromisoformat(report['business_date'])


def test_batch_preserves_prices_original_item_keys_and_per_order_salesperson(tmp_path, department_artifact):
    _, report, scope, day = department_artifact
    batch = load_department_batch(tmp_path, day, scope)
    assert batch.snapshot.source_versions['erp_department_preview'] == scope['preview_id']
    assert batch.snapshot.source_versions['erp_capture_id'] == scope['capture_id']
    assert batch.department == scope['department']
    for decision, raw in zip(batch.decisions, report['decisions'], strict=True):
        source = report['references']['lines'][decision.selected_row_id]
        price = batch.prices[decision.key.identity()]
        assert decision.key.model_dump(mode='json') == raw['key']
        assert price.upload_price == decision.upload_price
        assert price.target_actual == decision.target_actual
        assert price.shop_name == source['erp_shop']
        assert price.fulfillment == 'direct'
        # 第一个商品有甲、乙的两张订单；同店归属冲突时不能任选一人接收消息。
        assert price.salesperson == {'000001': None, '000002': '乙'}[source['order_id']]
        assert decision.trace['erp_department']['salesperson'] == price.salesperson


@pytest.mark.parametrize('issue', ['duplicate_target', 'wrong_shop', 'not_ready'])
def test_one_invalid_target_does_not_change_other_prices(tmp_path, department_artifact, issue):
    path, report, scope, day = department_artifact
    row = report['decisions'][0]
    if issue == 'duplicate_target':
        report['decisions'].append(deepcopy(row))
    elif issue == 'wrong_shop':
        report['references']['lines'][row['selected_row_id']]['target']['shop_id'] = 'other-shop'
    else:
        row.update(ready=False, status='不调价', reason='已有调价保护')
    path.write_text(json.dumps(report), encoding='utf-8')
    batch = load_department_batch(tmp_path, day, scope)
    assert len(batch.prices) == 1
    assert batch.decisions[0].ready is False
    assert batch.decisions[1].ready is True
    assert batch.prices[batch.decisions[1].key.identity()].salesperson == '乙'
    if issue == 'not_ready':
        assert batch.decisions[0].status == '不调价'
        assert batch.decisions[0].reason == '已有调价保护'
        assert batch.decisions[0].trace['erp_department']['salesperson'] is None
    else:
        assert batch.decisions[0].status == '待处理'


def test_synthetic_replay_and_previous_day_are_not_inputs(tmp_path, department_artifact):
    path, report, scope, day = department_artifact
    assert load_department_batch(tmp_path, day + timedelta(days=1), scope) is None
    assert load_department_batch(tmp_path, day, {**scope, 'mode': 'replay'}) is None
    report['synthetic'] = True
    path.write_text(json.dumps(report), encoding='utf-8')
    assert load_department_batch(tmp_path, day, scope) is None
