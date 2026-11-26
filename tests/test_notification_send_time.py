from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.adapters.dingtalk import DingTalkClient
from app.models.notifications import Notice
from app.services.notifications import dispatch_notices, enqueue_batch_notices
from app.services.store import BEIJING, stamp
from test_notifications import notices_context


@pytest.mark.parametrize('hour,minute,expected', [(5, 0, 0), (7, 59, 0), (8, 0, 1), (22, 0, 1)])
def test_pending_notice_waits_until_eight(notices_context, monkeypatch, hour, minute, expected):
    factory, config = notices_context
    config.ding_enabled = True
    now = datetime(2026, 11, 26, hour, minute, tzinfo=BEIJING)
    monkeypatch.setattr('app.services.notifications.utcnow', lambda: now)
    enqueue_batch_notices(factory, 'b')
    send = Mock(return_value={'state': 'accepted', 'task_id': 'task'})
    monkeypatch.setattr(DingTalkClient, 'send_notice', send)

    dispatch_notices(factory, config)

    assert send.call_count == expected
    with factory() as session:
        notice = session.scalar(select(Notice))
        assert notice.delivery_state == ('accepted' if expected else 'pending')
        assert bool(notice.delivery_evidence) == bool(expected)


def test_eight_oclock_releases_queue_in_creation_order(notices_context, monkeypatch):
    factory, config = notices_context
    config.ding_enabled = True
    now = datetime(2026, 11, 26, 7, 59, tzinfo=BEIJING)
    monkeypatch.setattr('app.services.notifications.utcnow', lambda: now)
    enqueue_batch_notices(factory, 'b')
    with factory.begin() as session:
        newer = session.scalar(select(Notice))
        newer.created_at = stamp(now)
        session.add(Notice(id='older', dedupe_key='older', recipient_id=newer.recipient_id,
                           batch_id=newer.batch_id, item_ids=list(newer.item_ids),
                           summary=dict(newer.summary), delivery_state='pending',
                           created_at=stamp(now - timedelta(minutes=5)), updated_at=stamp(now)))
    sent = []

    def send(self, *args):
        with factory() as session:
            sending = session.scalar(select(Notice).where(Notice.delivery_state == 'sending'))
            sent.append(sending.id)
        return {'state': 'accepted', 'task_id': f'task-{len(sent)}'}

    monkeypatch.setattr(DingTalkClient, 'send_notice', send)
    dispatch_notices(factory, config)
    assert sent == []

    now = now.replace(hour=8, minute=0)
    dispatch_notices(factory, config)
    assert len(sent) == 2
    assert sent[0] == 'older'


def test_receipt_queries_continue_before_eight(notices_context, monkeypatch):
    factory, config = notices_context
    config.ding_enabled = True
    now = datetime(2026, 11, 26, 6, tzinfo=BEIJING)
    monkeypatch.setattr('app.services.notifications.utcnow', lambda: now)
    enqueue_batch_notices(factory, 'b')
    with factory.begin() as session:
        notice = session.scalar(select(Notice))
        notice.delivery_state = 'accepted'
        notice.task_id = 'task'
        notice.recipient_userid = 'ding-seller'
        notice.updated_at = stamp(now - timedelta(hours=1))
    send = Mock(side_effect=AssertionError('accepted notice must not resend'))
    query = Mock(return_value={'state': 'read'})
    monkeypatch.setattr(DingTalkClient, 'send_notice', send)
    monkeypatch.setattr(DingTalkClient, 'query_notice', query)

    dispatch_notices(factory, config)

    send.assert_not_called()
    query.assert_called_once_with('task', 'ding-seller')
