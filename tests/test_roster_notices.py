from datetime import datetime, timezone

from app.config import Settings
from app.models import Job, Ownership, UserAccount
from app.worker import Worker
from test_worker import setup


def test_daily_roster_notifies_each_salesperson_once_without_project_accounts(tmp_path, monkeypatch):
    engine, factory, service = setup(tmp_path)
    service.config = Settings(
        _env_file=None, runtime_dir=tmp_path, coordination_required=False,
        ding_enabled=True, ding_client_id='test-robot', ding_client_secret='test-secret',
        ding_roster_userids={'桂露': '00123', '莫雯舒': '00456'},
    )
    sent = []

    def capture(config, *, roster_date):
        assert config.live_submit_enabled is False
        return {
            'status': 'needs_attention', 'ready_prices': 7,
            'salespersons': ['桂露', '莫雯舒', '赵亮亮'],
            'departments': [
                {'department': 'Tiktok一部', 'salesperson': '桂露', 'status': 'completed', 'ready_prices': 2},
                {'department': 'Tiktok二部', 'salesperson': '桂露', 'status': 'failed', 'ready_prices': 0},
                {'department': 'Tiktok一部', 'salesperson': '莫雯舒', 'status': 'completed', 'ready_prices': 5},
                {'department': 'Tiktok一部', 'salesperson': '赵亮亮', 'status': 'completed', 'ready_prices': 0},
            ],
        }

    def send(client, userid, title, markdown, url):
        assert title == '亏损提价处理结果'
        assert '待处理的明细：' in markdown
        assert '查询提价数据下载' in markdown
        sent.append((userid, markdown))
        return {'state': 'accepted', 'task_id': f'task-{userid}'}

    monkeypatch.setattr('app.services.erp_workflow.run_workflow', capture)
    monkeypatch.setattr('app.adapters.dingtalk.DingTalkClient.send_notice', send)
    worker = Worker(factory, service)
    now = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc).timestamp()
    for delay in range(0, 40, 5):
        worker.tick(now + delay)

    assert {userid for userid, _ in sent} == {'00123', '00456'}
    assert len(sent) == 2
    by_userid = dict(sent)
    assert '桂露' in by_userid['00123'] and '算价通过 2 条' in by_userid['00123']
    assert '莫雯舒' in by_userid['00456'] and '算价通过 5 条' in by_userid['00456']
    assert 'Tiktok二部' in by_userid['00123'] and 'Tiktok二部' not in by_userid['00456']
    assert all('未提交' in markdown for markdown in by_userid.values())
    with factory() as session:
        assert session.query(UserAccount).count() == session.query(Ownership).count() == 0
        jobs = session.query(Job).filter_by(kind='erp_roster_notice').all()
        assert {job.payload['salesperson']: job.payload['delivery']['state'] for job in jobs} == {
            '桂露': 'accepted', '莫雯舒': 'accepted', '赵亮亮': 'failed',
        }
    engine.dispose()


def test_roster_template_uses_only_persisted_personal_results_and_acceptance(tmp_path):
    from app.models import Batch, Item, Execution
    from app.services.roster_notices import roster_notice_content
    engine, factory, _ = setup(tmp_path)
    day, at = '2026-11-25', '2026-11-25T02:30:00+00:00'
    with factory.begin() as session:
        for ident, person, suffix in [('a', '桂露', ''), ('b', '桂露', 'stock:'), ('c', '莫雯舒', '')]:
            session.add(Batch(id=ident, run_key=f'erp-roster:job:{suffix}{person}:{day}',
                business_date=day, mode='live', status='completed', created_at=at,
                snapshot={'source_versions': {'erp_roster_salesperson': person}}, summary={}))
            session.flush()
            session.add(Item(id=ident, batch_id=ident, variant_id=ident,
                key={'sku': f'SKU-{ident}', 'site': 'MY', 'shop_id': ident},
                status='待核验' if ident == 'a' else '待处理', phase='done',
                reason=f'原因-{ident}', decision={}, updated_at=at))
            session.flush()
            if ident == 'a':
                session.add(Execution(id=ident, item_id=ident, variant_id=ident,
                    state='mismatch', request={}, response={'status': 'accepted'}, evidence={},
                    created_at=at, updated_at=at))
        session.flush()
        title, text = roster_notice_content(session,
            {'date': day, 'salesperson': '桂露', 'scopes': []}, at)
        assert title == '亏损提价处理结果'
        assert '共处理**2**条' in text
        assert '待处理 1 · 已提价 1 · 不调价 0' in text
        assert 'SKU-b' in text and '原因-b' in text
        assert 'SKU-c' not in text and '原因-c' not in text and '莫雯舒' not in text
        assert '查询提价数据下载' in text and '2026-11-25 10:30:00' in text

        first = session.get(Item, 'a')
        second = session.get(Item, 'b')
        second.key, second.decision = dict(first.key), dict(first.decision)
        second.updated_at = '2026-11-25T02:31:00+00:00'
        session.get(Execution, 'a').state = 'failed'
        session.get(Execution, 'a').response = {'status': 'rejected'}
        session.add(Execution(id='b', item_id='b', variant_id='b', state='verified',
            request={}, response={'status': 'accepted'}, evidence={}, created_at=at,
            updated_at=second.updated_at))
        session.flush()
        _, text = roster_notice_content(session,
            {'date': day, 'salesperson': '桂露', 'scopes': []}, at)
        assert '共处理**1**条' in text
        assert '待处理 0 · 已提价 1 · 不调价 0' in text
        assert '待处理的明细：\n\n无' in text

    engine.dispose()