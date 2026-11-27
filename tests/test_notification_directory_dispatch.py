import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.adapters.dingtalk import DingTalkClient
from app.adapters.dingtalk_directory import resolve_salesperson_userid
from app.models import Batch, Item, UserAccount
from app.models.notifications import Notice
from app.services.notifications import dispatch_notices, enqueue_batch_notices
from test_notifications import notices_context


def enable(config):
    config.ding_enabled = True


@pytest.mark.parametrize('department', [False, True])
def test_actual_dispatch_resolves_saved_erp_name_not_bound_userid(notices_context, monkeypatch, department):
    factory, config = notices_context
    enable(config)
    with factory.begin() as session:
        if department:
            session.get(Item, 'i').decision = {'trace': {'erp_department': {'salesperson': ' seller '}}}
        else:
            session.get(Batch, 'b').snapshot = {'source_versions': {'erp_roster_salesperson': ' seller '}}
        session.get(UserAccount, 'seller').dingtalk_userid = None
    resolver = Mock(wraps=resolve_salesperson_userid)
    monkeypatch.setattr('app.services.notifications.resolve_salesperson_userid', resolver)
    enqueue_batch_notices(factory, 'b')
    def send(self, userid, title, text, url):
        assert userid == 'ding-seller'
        with factory() as session:
            notice = session.scalar(select(Notice))
            assert notice.delivery_state == 'sending'
            assert notice.recipient_userid == userid
            assert notice.delivery_evidence[-1]['operation'] == 'send_intent'
        return {'state': 'accepted', 'task_id': 'task'}
    monkeypatch.setattr(DingTalkClient, 'send_notice', send)
    dispatch_notices(factory, config)
    resolver.assert_called_once_with(config, 'seller')
    # 回执查询使用实际提交的 User ID，不因目录变化重新选人或重发。
    (config.runtime_dir / 'dingtalk_users.json').write_text('{', encoding='utf-8')
    monkeypatch.setattr(DingTalkClient, 'send_notice', lambda *args: pytest.fail('must not resend'))
    query = Mock(return_value={'state': 'read'})
    monkeypatch.setattr(DingTalkClient, 'query_notice', query)
    dispatch_notices(factory, config)
    assert resolver.call_count == 1
    query.assert_called_once_with('task', 'ding-seller')


def test_dispatch_falls_back_to_authorized_account_name(notices_context, monkeypatch):
    factory, config = notices_context
    enable(config)
    resolver = Mock(wraps=resolve_salesperson_userid)
    monkeypatch.setattr('app.services.notifications.resolve_salesperson_userid', resolver)
    monkeypatch.setattr(DingTalkClient, 'send_notice', lambda self, userid, *args: {'state': 'accepted', 'task_id': userid})
    enqueue_batch_notices(factory, 'b')
    dispatch_notices(factory, config)
    resolver.assert_called_once_with(config, 'seller')
    with factory() as session:
        assert session.scalar(select(Notice)).recipient_userid == 'ding-seller'


@pytest.mark.parametrize('department,seller', [(False, 'other'), (True, 'other'), (True, None)])
def test_conflicting_erp_seller_does_not_send_to_account_owner(notices_context, monkeypatch, department, seller):
    factory, config = notices_context
    enable(config)
    with factory.begin() as session:
        if department:
            session.get(Item, 'i').decision = {'trace': {'erp_department': {'salesperson': seller}}}
        else:
            session.get(Batch, 'b').snapshot = {'source_versions': {'erp_roster_salesperson': seller}}
    enqueue_batch_notices(factory, 'b')
    resolver = Mock(side_effect=AssertionError('conflicting seller must not resolve'))
    monkeypatch.setattr('app.services.notifications.resolve_salesperson_userid', resolver)
    monkeypatch.setattr(DingTalkClient, 'send_notice', lambda *args: pytest.fail('must not disclose conflicting seller'))
    dispatch_notices(factory, config)
    with factory() as session:
        notice = session.scalar(select(Notice))
        assert notice.delivery_state == 'pending'
        assert 'ERP销售员' in notice.last_error
    resolver.assert_not_called()


def test_directory_ambiguity_leaves_notice_unsent(notices_context, monkeypatch):
    factory, config = notices_context
    enable(config)
    (config.runtime_dir / 'dingtalk_users.json').write_text(json.dumps({'users': [
        {'name': 'seller', 'user_id': userid, 'active': True} for userid in ('one', 'two')
    ]}), encoding='utf-8')
    enqueue_batch_notices(factory, 'b')
    monkeypatch.setattr(DingTalkClient, 'send_notice', lambda *args: pytest.fail('ambiguous recipient'))
    dispatch_notices(factory, config)
    with factory() as session:
        notice = session.scalar(select(Notice))
        assert notice.delivery_state == 'pending'
        assert not notice.recipient_userid
        assert '唯一' in notice.last_error


def test_admin_warning_uses_separate_target_without_directory(notices_context, monkeypatch):
    factory, config = notices_context
    enable(config)
    config.ding_alert_userid = 'alert-only'
    with factory.begin() as session:
        session.get(Item, 'i').decision = {'trace': {'warning_codes': ['test'], 'warnings': ['test-warning']}}
    enqueue_batch_notices(factory, 'b')
    with factory.begin() as session:
        regular = session.scalar(select(Notice).where(Notice.recipient_id == 'seller'))
        regular.delivery_state = 'cancelled'
    monkeypatch.setattr('app.services.notifications.resolve_salesperson_userid', lambda *args: pytest.fail('admin warning must not use directory'))
    sent = []
    def send(self, userid, *args):
        sent.append(userid)
        return {'state': 'accepted', 'task_id': 'alert-task'}
    monkeypatch.setattr(DingTalkClient, 'send_notice', send)
    dispatch_notices(factory, config)
    assert sent == ['alert-only']


def test_conflicting_item_does_not_block_other_authorized_items(notices_context, monkeypatch):
    factory, config = notices_context
    enable(config)
    with factory.begin() as session:
        batch = session.get(Batch, 'b')
        item = session.get(Item, 'i')
        session.add(Batch(id='conflicting', run_key='conflicting', business_date=batch.business_date,
            mode='live', status='done', created_at=batch.created_at,
            snapshot={'source_versions': {'erp_roster_salesperson': 'other'}}, summary={}))
        session.flush()
        session.add(Item(id='conflicting', batch_id='conflicting', variant_id='v2', key=dict(item.key),
            status='待处理', phase='failed', reason='other-person-detail', decision={}, updated_at=item.updated_at))
    enqueue_batch_notices(factory, 'b')
    with factory.begin() as session:
        notice = session.scalar(select(Notice))
        notice.item_ids = ['i', 'conflicting']
    sent = []
    def send(self, userid, title, text, url):
        sent.append((userid, text))
        return {'state': 'accepted', 'task_id': 'task'}
    monkeypatch.setattr(DingTalkClient, 'send_notice', send)
    dispatch_notices(factory, config)
    assert len(sent) == 1 and sent[0][0] == 'ding-seller'
    assert 'other-person-detail' not in sent[0][1]
    assert '共处理**1**条' in sent[0][1]


def test_escalation_resolves_authorized_supervisor_not_erp_seller(notices_context, monkeypatch):
    from datetime import timedelta
    from app.services.store import stamp, utcnow
    factory, config = notices_context
    enable(config)
    with factory.begin() as session:
        session.get(Batch, 'b').snapshot = {'source_versions': {'erp_roster_salesperson': 'seller'}}
    enqueue_batch_notices(factory, 'b')
    with factory.begin() as session:
        notice = session.scalar(select(Notice))
        notice.created_at = stamp(utcnow() - timedelta(hours=25))
    sent = []
    def send(self, userid, *args):
        sent.append(userid)
        return {'state': 'accepted', 'task_id': 'task-' + userid}
    monkeypatch.setattr(DingTalkClient, 'send_notice', send)
    dispatch_notices(factory, config)
    assert set(sent) == {'ding-seller', 'ding-manager'}


def test_department_sends_each_erp_seller_their_own_items_without_web_ownership(notices_context, monkeypatch):
    factory, config = notices_context
    enable(config)
    directory = {'users': [
        {'name': '石乔', 'user_id': 'staff-stone', 'active': True},
        {'name': '田雨', 'user_id': 'staff-rain', 'active': True},
    ]}
    (config.runtime_dir / 'dingtalk_users.json').write_text(json.dumps(directory), encoding='utf-8')
    with factory.begin() as session:
        session.get(Batch, 'b').run_key = 'erp-department:2026-11-05:Tiktok一部'
        original = session.get(Item, 'i')
        # 首个商品在网页上属于其他人，其余商品没有网页归属。
        original.reason = '证据甲'
        original.decision = {'trace': {'erp_department': {'salesperson': ' 石乔 '}}}
        for ident, name in [('乙', '田雨'), ('丙', '石乔'), ('丁', '查无此人')]:
            session.add(Item(id=ident, batch_id='b', variant_id='v'+ident,
                key={**original.key, 'shop_id': '未绑定', 'sku': ident}, status='待处理',
                phase='failed', reason='证据'+ident, updated_at=original.updated_at,
                decision={'trace': {'erp_department': {'salesperson': name}}}))
    sent = []
    def send(client, userid, title, message, url):
        sent.append((userid, message))
        return {'state': 'accepted', 'task_id': 'task-' + userid}
    monkeypatch.setattr(DingTalkClient, 'send_notice', send)
    monkeypatch.setattr(DingTalkClient, 'query_notice', lambda *args: {'state': 'read'})
    enqueue_batch_notices(factory, 'b')
    dispatch_notices(factory, config)
    enqueue_batch_notices(factory, 'b')
    dispatch_notices(factory, config)
    assert sorted(uid for uid, _ in sent) == ['staff-rain', 'staff-stone']
    cards = dict(sent)
    assert '共处理**2**条' in cards['staff-stone']
    assert '证据甲' in cards['staff-stone'] and '证据丙' in cards['staff-stone']
    assert '证据乙' not in cards['staff-stone'] and '证据丁' not in cards['staff-stone']
    assert '共处理**1**条' in cards['staff-rain'] and '证据乙' in cards['staff-rain']
    assert '证据甲' not in cards['staff-rain']
    with factory() as session:
        rows = list(session.scalars(select(Notice)))
        assert len(rows) == 3
        assert sorted(row.recipient_userid for row in rows if row.delivery_state == 'read') == ['staff-rain', 'staff-stone']
        missing = next(row for row in rows if row.delivery_state == 'pending')
        assert missing.item_ids == ['丁'] and '唯一' in missing.last_error


def test_department_notice_still_requires_item_access_in_the_web_inbox(notices_context):
    from app.services.coordination import Principal
    from app.services.notifications import notice_visible

    factory, _ = notices_context
    with factory.begin() as session:
        session.get(Batch, 'b').run_key = 'erp-department:2026-11-05:Tiktok一部'
        session.get(Item, 'i').decision = {'trace': {'erp_department': {'salesperson': 'seller'}}}
    enqueue_batch_notices(factory, 'b')
    with factory() as session:
        notice = session.scalars(select(Notice)).one()
        assert notice_visible(session, Principal('seller', 'seller', 'seller', 'sales'), notice)
        assert not notice_visible(session, Principal('other', 'other', 'seller', 'sales'), notice)


def test_department_waits_for_upload_then_reports_acceptance(notices_context, monkeypatch):
    from app.models import Execution
    factory, config = notices_context
    enable(config)
    with factory.begin() as session:
        session.get(Batch, 'b').run_key = 'erp-department:2026-11-05:Tiktok一部'
        item = session.get(Item, 'i')
        item.phase = 'proposed'
        item.decision = {'trace': {'erp_department': {'salesperson': 'seller'}}}
    sent = []
    monkeypatch.setattr(DingTalkClient, 'send_notice', lambda _, *args: sent.append(args) or {'state': 'accepted', 'task_id': 'task'})
    enqueue_batch_notices(factory, 'b')
    with factory() as session:
        ids = list(session.scalars(select(Notice.id)))
    dispatch_notices(factory, config, notice_ids=ids)
    assert not sent
    with factory.begin() as session:
        item = session.get(Item, 'i')
        item.phase, item.status = 'accepted', '待核验'
        session.add(Execution(id='execution', item_id=item.id, variant_id=item.variant_id,
            state='accepted', response={'status': 'accepted'}, request={}, evidence={},
            created_at=item.updated_at, updated_at=item.updated_at))
    dispatch_notices(factory, config, notice_ids=[])
    assert not sent
    dispatch_notices(factory, config, notice_ids=ids)
    assert len(sent) == 1 and '已提价 1' in sent[0][2] and '待处理 0' in sent[0][2]


def test_department_reports_unknown_without_waiting_for_result_or_other_salespeople(notices_context, monkeypatch):
    from app.notification_worker import _serve
    from threading import Event
    from types import SimpleNamespace

    factory, config = notices_context
    enable(config)
    with factory.begin() as session:
        session.get(Batch, 'b').run_key = 'erp-department:2026-11-05:Tiktok一部'
        item = session.get(Item, 'i')
        item.phase, item.status = 'unknown', '待核验'
        item.decision = {'trace': {'erp_department': {'salesperson': 'seller'}}}
        session.add(Item(id='next', batch_id='b', variant_id='next', key=item.key,
            status='待提价', phase='proposed', reason='', updated_at=item.updated_at,
            decision={'trace': {'erp_department': {'salesperson': 'manager'}}}))
    enqueue_batch_notices(factory, 'b')
    sent = []
    monkeypatch.setattr(DingTalkClient, 'send_notice', lambda _, *args: sent.append(args) or {'state': 'accepted', 'task_id': 'task'})
    stop = Event()
    _serve(factory, config, SimpleNamespace(is_set=stop.is_set, wait=lambda _: stop.set()))
    assert len(sent) == 1 and sent[0][0] == 'ding-seller' and '待处理 1' in sent[0][2]
