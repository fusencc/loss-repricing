"""日期名单只控制 ERP 只读流程，不授权提交、不允许空名单退回全量。"""
from datetime import date, datetime
import json
import pytest
from app.config import Settings
from app.services import erp_workflow as workflow


DAY = date(2026, 11, 25)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 25, 9, 0, tzinfo=tz)
    monkeypatch.setattr(workflow, 'datetime', Clock)
    return Settings(_env_file=None, runtime_dir=tmp_path, erp_roster_date=DAY)


@pytest.mark.parametrize('value', [None, (), (' ',), ('甲', None), {}, '甲'])
def test_bad_roster_stops_before_login(settings, monkeypatch, value):
    monkeypatch.setattr(workflow, 'ERP_ROSTER_NAMES', value)
    monkeypatch.setattr(workflow, '_session', lambda *_: pytest.fail('名单无效不得登录'))
    with pytest.raises(ValueError):
        workflow.run_workflow(settings)


def test_roster_only_passes_people_to_their_confirmed_department(settings, monkeypatch):
    monkeypatch.setattr(workflow, 'ERP_ROSTER_NAMES', (' 甲 ', '乙', '甲'))
    monkeypatch.setattr(workflow, 'ERP_ROSTER_DEPARTMENTS', {'甲': 'Tiktok一部', '乙': 'Tiktok五部'})
    calls = []
    def collect(settings, *, department, salesperson, **kwargs):
        calls.append((department, salesperson))
        if salesperson == '甲':
            raise ValueError('单个范围失败')
        return {'capture_id': 'saved', 'capture_complete': True, 'groups': [],
                'cleaning_status': 'saved', 'department': department}
    monkeypatch.setattr(workflow, 'capture_erp_bulk', collect)
    monkeypatch.setattr(workflow, 'save_capture', lambda runtime, result: result)
    monkeypatch.setattr(workflow, 'refresh_daily_fees', lambda *a, **k: {})
    monkeypatch.setattr(workflow, 'query_current_erp_fx', lambda *a: {'ref': 'test-fx'})
    result = workflow.run_workflow(settings, roster_date=DAY)
    assert calls == [('Tiktok一部', '甲'), ('Tiktok五部', '乙')]
    assert result['salespersons'] == ['甲', '乙']
    assert result['roster_date'] == str(DAY)
    assert result['status'] == 'needs_attention'
    assert not result['execution_allowed']


@pytest.mark.parametrize('departments', [{}, {'甲': '未确认部门'}])
def test_roster_without_valid_department_stops_before_login(settings, monkeypatch, departments):
    monkeypatch.setattr(workflow, 'ERP_ROSTER_NAMES', ('甲',))
    monkeypatch.setattr(workflow, 'ERP_ROSTER_DEPARTMENTS', departments)
    monkeypatch.setattr(workflow, '_session', lambda *_: pytest.fail('部门归属无效不得登录'))
    with pytest.raises(ValueError, match='缺少有效部门归属'):
        workflow.run_workflow(settings)


def test_explicit_date_mismatch_stops(settings, monkeypatch):
    monkeypatch.setattr(workflow, '_session', lambda *_: pytest.fail('日期不符不得登录'))
    with pytest.raises(ValueError, match='业务日'):
        workflow.run_workflow(settings, roster_date=date(2026, 11, 26))


def test_replay_cannot_be_used_with_roster(settings):
    with pytest.raises(ValueError, match='离线回放'):
        workflow.run_workflow(settings, capture_id='unused', roster_date=DAY)


def test_confirmed_roster_is_shipped_in_code_without_local_files(settings):
    assert not (settings.runtime_dir / 'erp-rosters').exists()
    assert workflow._roster(settings, DAY) == [
        '桂露', '莫雯舒', '赵亮亮', '刘付文昌', '李佳丽', '杨颖', '曹雪娇',
    ]
    defaults = Settings(_env_file=None)
    assert defaults.erp_roster_date == DAY
    assert defaults.erp_roster_schedule_enabled is True


def test_local_file_cannot_replace_code_roster(settings):
    folder = settings.runtime_dir / 'erp-rosters'
    folder.mkdir()
    (folder / f'{DAY}.json').write_text(json.dumps(['其他人员']), encoding='utf-8')
    assert workflow._roster(settings, DAY) == list(workflow.ERP_ROSTER_NAMES)


def test_other_days_not_implicitly_restricted(settings):
    settings.erp_roster_date = date(2026, 11, 26)
    assert workflow._roster(settings, None) is None
