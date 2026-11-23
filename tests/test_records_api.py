import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.models import Batch, Item, Execution, Ownership, UserAccount
from app.services.coordination import scope_key
from app.domain.types import VariantKey


def key(index):
    return {'platform': 'tiktok', 'shop_id': f'SHOP-{index}', 'site': 'MY',
            'product_id': '001', 'item_id': '002', 'sku': f'SKU-{index}', 'warehouse': 'local'}


@pytest.fixture
def client(tmp_path):
    cfg = Settings(database_url=f"sqlite:///{tmp_path/'records.db'}", runtime_dir=tmp_path,
        coordination_required=False, admin_password='test-local-password',
        session_secret='test-session-secret-32-characters-long', _env_file=None)
    with TestClient(create_app(cfg)) as client:
        with client.app.state.factory.begin() as session:
            for i, department in enumerate(['Tiktok一部', 'Tiktok五部', None, 'Tiktok一部']):
                day = ['2025-10-31', '2026-11-01', '2026-11-13', '2026-11-17'][i]
                session.add(Batch(id=f'b{i}', run_key=f'b{i}', business_date=day, mode='demo',
                    status='completed', created_at=f'{day}T01:00:00+00:00', snapshot={}, summary={}))
                session.flush()
                session.add(Item(id=f'i{i}', batch_id=f'b{i}', variant_id=f'v{i}', key=key(i),
                    status='已核验' if i == 3 else '待处理', phase='done', reason='测试处理说明',
                    decision={'current_actual': '12.00', 'target_actual': '15.123456789123456789',
                        'upload_price': '18.00', 'trace': {'station_currency': 'MYR', 'online_rows': [
                            {'key': key(i), 'profit_check': {'department': department}}]}},
                    updated_at=f'{day}T02:00:00+00:00'))
            for ident in ['seller-1', 'seller-2']:
                session.add(UserAccount(id=ident, username=ident, display_name='同名销售', role='sales',
                    active=True, password_hash='private-password', dingtalk_userid='private-'+ident,
                    created_at='2026-01-01T00:00:00+00:00', updated_at='2026-01-01T00:00:00+00:00'))
            for i in [0, 3]:
                k = key(i)
                session.add(Ownership(id=f'o{i}', scope_key=scope_key(k['platform'], k['shop_id'], k['site']),
                    platform=k['platform'], shop_id=k['shop_id'], site=k['site'],
                    owner_id='seller-1', autonomous=False))
        yield client


def test_first_margin_exposes_only_selected_erp_order_amount(client):
    with client.app.state.factory.begin() as session:
        item = session.get(Item, 'i3')
        row = {'row_id': 'selected', 'basis': 'order', 'fee_scope': 'erp-corrected-first-margin-v1',
               'loss_amount': '-12.3000', 'currency': 'CNY', 'order_id': 'private-order'}
        item.decision = dict(item.decision, selected_row_id='selected', unit_loss='4.10',
                             trace=dict(item.decision['trace'], source_rows=[dict(row, row_id='other'), row]))
    rows = {row['id']: row for row in client.get('/api/records').json()['items']}
    assert rows['i3']['first_margin'] == {'amount': '-12.3000', 'currency': 'CNY'}
    assert rows['i0']['first_margin'] is None


def test_record_projection_exposes_optional_saved_reference_without_changing_status(client):
    from test_sku_price_reference import reference, sample
    ref=reference([sample(s, 1, '100', '50') for s in 'ABC']).model_dump(mode='json')
    with client.app.state.factory.begin() as session:
        item=session.get(Item, 'i3')
        item.decision=dict(item.decision, trace=dict(item.decision['trace'], sku_price_reference=ref,
            price_limit={'upper_actual':'50','applied':True,'reduction_amount':'1.2'}))
    rows={r['id']:r for r in client.get('/api/records').json()['items']}
    assert rows['i3']['decision']['sku_price_reference']['marketing_price']=='50'
    assert len(rows['i3']['decision']['sku_price_reference']['samples'])==3
    assert rows['i3']['decision']['price_limit']['reduction_amount']=='1.2'
    assert rows['i3']['status']=='已核验'
    assert rows['i0']['decision'].get('sku_price_reference') is None


def test_department_records_are_public_but_management_remains_protected(client):
    response = client.get('/api/records')
    assert response.status_code == 200
    data = response.json()
    assert [d['name'] for d in data['departments']] == ['Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部', '待确认部门']
    assert data['department'] == 'department:Tiktok一部'
    assert {r['id'] for r in data['items']} == {'i0', 'i3'}
    assert data['total'] == 2 and data['shops'] == 2
    assert data['counts'] == {'待处理': 1, '已核验': 1}
    assert data['items'][0]['id'] == 'i3'
    assert client.get('/api/auth/me').status_code == 401
    assert client.get('/api/settings').status_code == 401
    assert client.get('/api/items/i3').status_code == 401
    assert client.post('/api/items/i3/reevaluate', json={}).status_code == 401


def test_department_switch_and_unassigned_are_explicit(client):
    data = client.get('/api/records', params={'department': 'department:Tiktok五部'}).json()
    assert [r['id'] for r in data['items']] == ['i1']
    assert data['items'][0]['seller'] == {'id': None, 'name': '待分配'}
    data = client.get('/api/records', params={'department': 'unassigned'}).json()
    assert [r['id'] for r in data['items']] == ['i2']
    assert data['items'][0]['department'] is None
    assert client.get('/api/records', params={'department': 'department:不存在'}).status_code == 404


def test_all_history_and_more_than_one_hundred_records_are_returned(client):
    with client.app.state.factory.begin() as session:
        for i in range(125):
            session.add(Item(id=f'extra-{i}', batch_id='b0', variant_id=f'extra-{i}', key=key(0),
                status='待核验', phase='lookup', reason='等待回读',
                decision={'trace': {'online_rows': [{'key': key(0), 'profit_check': {'department': 'Tiktok一部'}}]}},
                updated_at='2025-10-31T03:00:00+00:00'))
    data = client.get('/api/records').json()
    assert data['total'] == 127 and len(data['items']) == 127
    assert len({r['id'] for r in data['items']}) == 127
    assert data['counts']['待核验'] == 125


def test_target_ownership_wins_and_duplicate_names_keep_distinct_ids(client):
    k = key(3)
    target = VariantKey.model_validate(k).target_identity()
    with client.app.state.factory.begin() as session:
        session.add(Ownership(id='override', scope_key=scope_key(k['platform'], k['shop_id'], k['site'], target),
            platform=k['platform'], shop_id=k['shop_id'], site=k['site'], target_id=target,
            owner_id='seller-2', autonomous=False))
    rows = {r['id']: r for r in client.get('/api/records').json()['items']}
    assert rows['i0']['seller'] == {'id': 'seller-1', 'name': '同名销售'}
    assert rows['i3']['seller'] == {'id': 'seller-2', 'name': '同名销售'}


def test_department_requires_matching_complete_nonconflicting_source(client):
    with client.app.state.factory.begin() as session:
        item = session.get(Item, 'i0')
        item.decision = {'trace': {'online_rows': [
            {'key': key(0), 'profit_check': {'department': 'Tiktok一部'}},
            {'key': key(0), 'profit_check': {'department': 'Tiktok五部'}},
            {'key': key(1), 'profit_check': {'department': '别的商品部门'}},
        ]}}
        item = session.get(Item, 'i3')
        item.decision = {'trace': {'online_rows': [
            {'key': key(3), 'profit_check': {'department': 'Tiktok一部'}},
            {'key': key(3), 'profit_check': None},
        ]}}
    data = client.get('/api/records', params={'department': 'unassigned'}).json()
    assert {r['id'] for r in data['items']} == {'i0', 'i2', 'i3'}
    assert '别的商品部门' not in str(data['departments'])


def test_public_projection_keeps_price_evidence_but_no_private_payload(client):
    with client.app.state.factory.begin() as session:
        item = session.get(Item, 'i3')
        item.decision = {**item.decision, 'unit_loss': 'private-cost'}
        item.key = {**item.key, 'internal_token': 'private-token'}
        session.add(Execution(id='e3', item_id='i3', variant_id='v3', state='verified', task_id='task-3',
            request={'upload_price': '18.00', 'secret': 'private-request'}, response={'secret': 'private-response'},
            evidence={'actual_price': '15.12', 'confirmed_at': '2026-11-17T03:00:00+00:00',
                'effective_at': '2026-11-17T03:00:00+00:00', 'effective_time_source': 'confirmed_at',
                'private': 'private-evidence', 'price_record': {'shop_name': '示例店铺', 'raw': 'private-raw',
                    'prices': {'tax_exclusive': {'amount': '16.50', 'currency': 'MYR',
                        'observed_at': '2026-11-17T03:00:00+00:00', 'raw': 'private-raw'}}}},
            created_at='2026-11-17T02:00:00+00:00', updated_at='2026-11-17T03:00:00+00:00'))
    response = client.get('/api/records')
    assert response.status_code == 200
    row = next(r for r in response.json()['items'] if r['id'] == 'i3')
    assert row['decision']['target_actual'] == '15.123456789123456789'
    assert row['execution']['uploaded_price'] == '18.00'
    assert row['execution']['actual_price'] == '15.12'
    assert row['execution']['effective_time_source'] == 'confirmed_at'
    # 用户已确认记录页不展示四价存档：投影不再返回 prices，原始证据仍在数据库。
    assert 'prices' not in row['execution']
    assert row['synthetic'] is True
    assert 'private' not in response.text
    assert 'internal_token' not in row['key']


def test_price_change_is_target_minus_current_with_decimal_precision(client):
    """价格变动 = 目标实际价 − 调整前实际价，Decimal 保存精度；任一价缺失则不显示。"""
    rows = {row['id']: row for row in client.get('/api/records').json()['items']}
    # i3 保存的 12.00 → 15.123456789123456789；十进制差值完整保留，不截断不四舍五入。
    assert rows['i3']['decision']['price_change'] == '3.123456789123456789'
    with client.app.state.factory.begin() as session:
        item = session.get(Item, 'i0')
        item.decision = {**item.decision, 'current_actual': '12.5', 'target_actual': '11.25'}
        missing = session.get(Item, 'i1')
        missing.decision = {**missing.decision, 'current_actual': None, 'target_actual': '15.00'}
    rows = {row['id']: row for row in client.get('/api/records').json()['items']}
    assert rows['i0']['decision']['price_change'] == '-1.25'
    # i1 属于 Tiktok五部；缺调整前实际价时不显示价格变动，不猜测。
    rows_wu = {row['id']: row for row in client.get('/api/records', params={'department': 'department:Tiktok五部'}).json()['items']}
    assert rows_wu['i1']['decision']['price_change'] is None


def test_price_stages_split_loss_and_profit_check_steps(client):
    """三环节：① 原实际营销价 ② 补亏损 ③ 利润校验，折合人民币与环比都由后端算好。"""
    with client.app.state.factory.begin() as session:
        batch = session.get(Batch, 'b3')
        batch.snapshot = {'fx_rates': {'MYR': '0.65', 'CNY': '1'}, 'fx_rate_unit': 'cny_per_currency',
                          'fx_date': '2026-11-17'}
        item = session.get(Item, 'i3')
        item.decision = dict(item.decision, current_actual='14', target_actual='34.868', station_loss='11.9602',
                             trace=dict(item.decision['trace'], online_profit={
                                 'initial_target': '25.9602', 'correction_amount': '8.9078',
                                 'initial_profit': '-0.8000', 'initial_margin': '-0.0308164'}))
    rows = {row['id']: row for row in client.get('/api/records').json()['items']}
    stages = rows['i3']['decision']['stages']
    assert stages['currency'] == 'MYR' and stages['fx_rate'] == '0.65'
    assert [(row['label'], row['price']) for row in stages['rows']] == [
        ('① 原实际营销价', '14.0000'), ('② 补亏损', '25.9602'), ('③ 利润校验', '34.8680')]
    # 每1MYR折0.65元；增量与涨幅相对上一环节，正数由前端补 + 号。
    assert [row['cny'] for row in stages['rows']] == ['9.1000', '16.8741', '22.6642']
    assert [row['change_cny'] for row in stages['rows']] == [None, '7.7741', '5.7901']
    assert [row['change_percent'] for row in stages['rows']] == [None, '85.43', '34.31']
    # 在线利润率导致的变动只出现在 ③ 利润校验行：校正额 8.9078 MYR × 0.65 = 5.7901 元。
    assert [row['profit_change_cny'] for row in stages['rows']] == [None, None, '5.7901']
    assert stages['rows'][1]['trial_profit'] == '-0.8000'
    assert stages['rows'][1]['trial_margin_percent'] == '-3.08'
    assert all('trial_margin_percent' not in stages['rows'][index] for index in (0, 2))


def test_price_stages_never_invent_the_loss_step_or_fx_rate(client):
    """没有保存补亏环节或汇率的记录：该行留空，不用目标价顶替。"""
    rows = {row['id']: row for row in client.get('/api/records').json()['items']}
    stages = rows['i3']['decision']['stages']
    assert stages['rows'][1]['price'] is None and stages['rows'][1]['change_cny'] is None
    assert 'trial_profit' not in stages['rows'][1] and 'trial_margin_percent' not in stages['rows'][1]
    # 上一环节缺失时「比上一环节」留空，不跨行比较制造环比。
    assert stages['rows'][2]['change_cny'] is None and stages['rows'][2]['change_percent'] is None
    # 没有保存在线利润校正额时该列留空，不把缺证据当 0。
    assert stages['rows'][2]['profit_change_cny'] is None
    assert stages['rows'][0]['cny'] is None
    with client.app.state.factory.begin() as session:
        batch = session.get(Batch, 'b3')
        # 旧快照未标注单位时是「每1CNY可兑换的站点币」，折人民币要相除。
        batch.snapshot = {'fx_rates': {'MYR': '1.5385', 'CNY': '1'}, 'fx_date': '2026-11-17'}
    stages = {row['id']: row for row in client.get('/api/records').json()['items']}['i3']['decision']['stages']
    # 该记录原始价 12.00 MYR；12 ÷ 1.5385 = 7.7998 元。
    assert stages['rows'][0]['cny'] == '7.7998'


def test_reference_stage_shows_decrease_without_claiming_profit_check_passed():
    from app.services.records import price_stages
    trace={'station_currency':'MYR','price_limit':{'upper_actual':'9'},
           'online_profit':{'initial_target':'11.2','correction_amount':None}}
    stages=price_stages({'current_actual':'10','target_actual':'9'},trace,
                       {'fx_rates':{'MYR':'1'},'fx_rate_unit':'cny_per_currency'},key(0))
    final=stages['rows'][2]
    assert final['label']=='③ 参考价优先'
    assert final['price']=='9.0000' and final['change_cny']=='-2.2000'
    assert final['profit_change_cny'] is None


def test_empty_database_is_a_valid_empty_page(client):
    with client.app.state.factory.begin() as session:
        session.query(Item).delete()
    data = client.get('/api/records').json()
    assert [d['name'] for d in data['departments']] == ['Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部']
    assert all(d['total'] == 0 for d in data['departments'])
    assert data['department'] == 'department:Tiktok一部'
    assert data['total'] == 0 and data['items'] == []
    assert client.get('/api/records', params={'department': 'department:Tiktok二十二部'}).json()['items'] == []


def test_late_verification_of_older_batch_is_the_latest_record(client):
    with client.app.state.factory.begin() as session:
        session.get(Item, 'i0').updated_at = '2026-11-18T09:00:00+00:00'
    data = client.get('/api/records').json()
    assert data['items'][0]['id'] == 'i0'
    assert data['items'][0]['business_date'] == '2025-10-31'


def test_live_records_normalize_source_department_and_exclude_demo(client):
    with client.app.state.factory.begin() as session:
        batch = session.get(Batch, 'b3')
        batch.mode = 'live'
        item = session.get(Item, 'i3')
        item.decision = {'trace': {'online_rows': [{'key': key(3), 'currency': 'PHP', 'profit_check': {'department': 'TIKTOK十二部'}}]}}
    from app.services.records import department_records
    with client.app.state.factory() as session:
        data = department_records(session, 'department:Tiktok十二部', mode='live')
    assert data['total'] == 1
    assert data['items'][0]['id'] == 'i3'
    assert data['items'][0]['department'] == 'Tiktok十二部'
    assert data['items'][0]['synthetic'] is False

    assert data['items'][0]['decision']['trace']['station_currency'] == 'PHP'


def test_preparations_and_failed_attempts_belong_to_one_successful_price_change():
    from copy import deepcopy
    from app.services.records import combine_pricing_process
    def row(ident, state, hour, shop='shop'):
        return {'id': ident, 'key': dict(key(1), shop_id=shop), 'mode': 'live',
                'business_date': '2026-11-17', 'updated_at': f'2026-11-17T{hour}:00:00+00:00',
                'decision': {'current_actual': '14', 'target_actual': '34.872', 'upload_price': '87.18'},
                'execution': {'state': state} if state else None, 'reason': ident,
                'status': '已核验' if state == 'verified' else '待处理'}
    rows = [row('success', 'verified', '10'), row('retry', None, '09'),
            row('rejected', 'failed', '08'), row('prepare', None, '07')]
    result = combine_pricing_process(deepcopy(rows))
    assert len(result) == 1 and result[0]['id'] == 'success'
    assert [p['stage'] for p in result[0]['process']] == ['准备提价', '提交失败', '准备提价', '提价成功']
    assert len(combine_pricing_process(deepcopy(rows) + [row('other-shop', None, '08', 'other')])) == 2
    # 未知提交不能隐藏；成功后的新一轮准备不能隐藏；两次成功不能视为一次。
    assert len(combine_pricing_process(deepcopy(rows) + [row('unknown', 'unknown', '09')])) == 2
    assert len(combine_pricing_process(deepcopy(rows) + [row('later', None, '11')])) == 2
    assert len(combine_pricing_process(deepcopy(rows) + [row('second-success', 'verified', '11')])) == 5
