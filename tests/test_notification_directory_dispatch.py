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
