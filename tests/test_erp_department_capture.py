"""集团下载、部门归档与回放只使用临时目录和合成 Excel。"""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path

from openpyxl import Workbook
import pytest

from app.adapters import erp_bulk_capture as bulk
from app.config import Settings
from app.services.erp_capture_store import load_capture, save_capture
from app.services.erp_department_capture import department_captures
from app.services.erp_pricing import preview_pricing


A, B = bulk.ERP_DEPARTMENTS[:2]


def sheet(rows, logistics=None):
    book = Workbook()
    book.active.append(['订单编号', '订单状态', '平台', '集团', '部门', '店铺', '毛利', '修正毛利', '修正一次毛利', '自选物流'])
    for order, department in rows:
        book.active.append([order, '可配货', 'Tiktok', '集团二部', department, '测试店铺', '-1', '-1', '-1', (logistics or {}).get(order)])
    buf = BytesIO()
    book.save(buf)
    book.close()
    return buf.getvalue()


def collect(tmp_path, partial, *, logistics=None):
    requests = []
    source = None
    current_ids = []

    def post(path, body, **kwargs):
        nonlocal source, current_ids
        if path in (bulk.LOSS_EXPORT, bulk.BLOCKED_LIST):
            assert 'department' not in body and 'departments' not in body
            source = 'loss' if path == bulk.LOSS_EXPORT else 'blocked'
            if source == 'loss':
                ids = ['b-first', 'shared', 'a-only'] if body['status'] == 4 else []
                if partial and ids:
                    ids += ['conflict', 'unknown', 'missing', 'failed-order']
                # 名单里的部门并非最终归属。
                return sheet([(order, '名单部门') for order in ids])
            ids = ['shared'] if body['status'] == 4 else []
            if partial and ids:
                ids += ['conflict', 'failed-order', 'a-only']
            return {'total': len(ids), 'records': [dict(orderid=order, platform='Tiktok', status=4, jzfh='1') for order in ids]}
        if path == bulk.ORDER_EXPORT:
            current_ids = body['orderIdList']
            requests.append((source, current_ids))
            if partial and (current_ids == ['failed-order'] or source == 'blocked' and 'a-only' in current_ids):
                raise bulk.SourceUnavailable('模拟财务导出失败')
            return '下载任务【合成任务】'
        assert path == bulk.TASKS
        return [dict(fileName='合成任务', source='orderList', id=str(len(requests)), status='success', downloadUrl='/mock.xlsx')]

    def download(path):
        assert path == '/mock.xlsx'
        return sheet([(order, {'b-first': B, 'conflict': B if source == 'loss' else A, 'unknown': '未参与部门'}.get(order, A.upper()))
                      for order in current_ids if order != 'missing'],
                     logistics=(logistics or {}).get(source))

    parent = bulk.capture_erp_bulk(Settings(_env_file=None, runtime_dir=tmp_path), web_session=(post, download),
                                   export_batch_size=1 if partial else 2)
    return save_capture(tmp_path, dict(parent, synthetic=True))


@pytest.mark.parametrize('partial', [False, True])
def test_split_actual_group_download_and_replay(tmp_path, partial):
    parent = collect(tmp_path, partial)
    originals = list(tmp_path.rglob('*.xlsx'))
    prior = deepcopy(parent)
    captures, unresolved = department_captures(parent, bulk.ERP_DEPARTMENTS)
    assert parent == prior
    assert unresolved == ({'conflict': 'department_conflict', 'unknown': 'unknown_department',
                           'missing': 'missing_order_finance', 'failed-order': 'missing_order_finance'} if partial else {})
    a = captures[A]
    assert a['observed_sources']['shared'] == ['loss_allocatable', 'blocked_allocatable']
    if partial:
        assert a['observed_sources']['a-only'] == ['loss_allocatable', 'blocked_allocatable']
    assert a['summary']['unique_orders'] == 2 and captures[B]['summary']['unique_orders'] == 1
    assert a['summary']['list_rows'] == 3 and a['summary']['loss_rows'] == 2
    assert a['department_parent_capture_id'] == parent['capture_id'] and 'result_file' not in a and 'capture_id' not in a
    assert {o for g in a['groups'] for o in g['candidate_order_ids']} == {'shared', 'a-only'}
    loss, _, blocked, _ = a['groups']
    assert blocked['candidate_order_ids'] == ['shared']
    assert blocked['filters'] == parent['groups'][2]['filters']
    assert all(not g['files'] for g in a['groups'])
    assert not captures[bulk.ERP_DEPARTMENTS[-1]]['summary']['unique_orders']
    batch = loss['batches'][0]
    actual = parent['groups'][0]['batches'][batch['parent_number'] - 1]
    assert actual['task_id'] == batch['task_id'] and actual['polls'] == batch['polls']
    assert batch['number'] == 1 and batch['order_ids'] == ['shared']
    assert set(actual['order_ids']) >= set(batch['order_ids'])
    saved = save_capture(tmp_path, a)
    assert saved['cleaning_status'] == 'saved'
    replay = load_capture(tmp_path, saved['capture_id'])
    report = preview_pricing(replay)
    assert report['consistent_capture_scope'] and report['evaluation_complete_sources'] == list(bulk.SOURCES)
    assert len(report['references']['order_finance']['shared']) == 2
    assert [r['finance_complete'] for r in report['source_coverage']] == [not partial, True, not partial, True]
    assert list(tmp_path.rglob('*.xlsx')) == originals
    assert load_capture(tmp_path, parent['capture_id'])['groups'] == prior['groups']
    # 派生档使用集团请求字段，却仍逐行核对财务部门；直接采集的历史档语义不变。
    replay['groups'][0]['rows'][0]['fields']['部门'] = B
    report = preview_pricing(replay)
    assert 'loss_allocatable' in report['evaluation_complete_sources']
    assert not report['source_coverage'][0]['finance_complete']
    assert any(i['code'] == 'finance_scope_mismatch' and i['order_id'] == 'shared' for i in report['issues'])
    replay['groups'][0]['filters']['department'] = A
    assert not preview_pricing(replay)['consistent_capture_scope']


def test_partial_parent_batch_and_failed_source_keep_upstream_status(tmp_path):
    parent = collect(tmp_path, partial=False)
    loss, _, blocked, _ = parent['groups']
    batch = loss['batches'][0]
    batch['order_ids'].append('not-returned')
    loss['candidate_order_ids'].append('not-returned')
    batch.update(status='partial', scope_check=bulk.finance_batch_scope(batch['order_ids'], ['b-first', 'shared']))
    loss.update(stage='complete_with_missing', missing_order_ids=['not-returned'], error='缺少一单')
    blocked.update(rows=[], batches=[], candidate_order_ids=[], list_complete=False, total=None,
                   stage='candidate_list_incomplete', error='模拟名单失败', failed_pages=[1])
    parent.update(capture_complete=False, selection_complete=False)
    captures, unresolved = department_captures(parent, [A, B])
    assert unresolved == {'not-returned': 'missing_order_finance'}
    a = captures[A]
    assert not a['capture_complete'] and a['groups'][2]['total'] is None
    assert a['groups'][2]['error'] == blocked['error'] and not a['groups'][2]['list_complete']
    view = a['groups'][0]['batches'][0]
    assert view['status'] == 'partial' and view['scope_check']['missing'] == []
    assert view['order_ids'] == ['shared']
    report = preview_pricing(save_capture(tmp_path, a))
    assert report['evaluation_complete_sources'] == ['loss_allocatable', 'loss_pending', 'blocked_pending']
    assert report['source_coverage'][0]['finance_complete'] is False


def test_department_archive_prices_with_offline_evidence(tmp_path):
    fixtures = Path(__file__).parent / 'fixtures'
    parent = json.loads((fixtures / 'erp-pricing-capture.sample.json').read_text())
    evidence = json.loads((fixtures / 'erp-pricing-evidence.sample.json').read_text())
    for group in parent['groups']:
        for row in group['rows']:
            row['fields']['部门'] = A
    for item in evidence['online']:
        item['state']['profit_check']['department'] = A
    children, unresolved = department_captures(parent, [A, B])
    assert not unresolved and children[B]['summary']['unique_orders'] == 0
    saved = save_capture(tmp_path, children[A])
    evidence['capture_id'] = saved['capture_id']
    report = preview_pricing(load_capture(tmp_path, saved['capture_id']), evidence)
    assert report['summary']['ready_prices'] > 0
    assert report['evaluation_complete_sources'] == list(bulk.SOURCES)


@pytest.mark.parametrize("shipping", ["Global economy shipping_来赞宝", "Global standard shipping_来赞宝"])
def test_sample_logistics_in_one_erp_export_excludes_order_from_every_department_source(tmp_path, shipping):
    parent = collect(tmp_path, partial=False, logistics={
        "loss": {"shared": shipping, "a-only": "Global standard shipping"},
    })
    original = deepcopy(parent)
    row = next(row for row in parent["groups"][0]["rows"] if row["orderid"] == "shared")
    assert row["fields"].get("自选物流") == shipping

    departments, unresolved = department_captures(parent, [A, B])
    assert not unresolved
    assert parent == original
    assert set(departments[A]["observed_sources"]) == {"a-only"}
    assert set(departments[B]["observed_sources"]) == {"b-first"}
    report = preview_pricing(save_capture(tmp_path, departments[A]))
    assert report["summary"]["unique_orders"] == 1
    assert "shared" not in report["references"]["order_finance"]
