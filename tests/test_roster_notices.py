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
