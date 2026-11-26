import json
from types import SimpleNamespace

import pytest

from app.adapters.dingtalk_directory import resolve_salesperson_userid


def resolve(tmp_path, rows, name='新增销售员'):
    (tmp_path / 'dingtalk_users.json').write_text(json.dumps({'users': rows}), encoding='utf-8')
    return resolve_salesperson_userid(SimpleNamespace(runtime_dir=tmp_path), name)


def user(name='新增销售员', userid='00123', active=True):
    return {'name': name, 'user_id': userid, 'active': active}


def test_directory_matches_exact_active_name_and_preserves_id(tmp_path):
    assert resolve(tmp_path, [user(), user(active=False, userid='999')]) == {'userid': '00123'}
    assert resolve(tmp_path, [user(), user()]) == {'userid': '00123'}
    assert resolve(tmp_path, [user(' 新增销售员 ')], ' 新增销售员 ') == {'userid': '00123'}


@pytest.mark.parametrize('rows', [
    [], [user(active=False)], [user('新增销售员甲')],
    [user(), user(userid='00456')], [user(), user(name='另一人')],
    [user(userid=123)], [user(userid='')], [user(), user(userid='')],
])
def test_directory_does_not_guess_ambiguous_or_missing_recipient(tmp_path, rows):
    assert resolve(tmp_path, rows)['state'] == 'failed'


def test_directory_reloads_personnel_changes(tmp_path):
    assert resolve(tmp_path, [user()]) == {'userid': '00123'}
    assert resolve(tmp_path, [user(userid='00999')]) == {'userid': '00999'}


@pytest.mark.parametrize('contents', [None, '{', '{}', '{"users": null}'])
def test_unavailable_directory_is_local_failure(tmp_path, contents):
    if contents is not None:
        (tmp_path / 'dingtalk_users.json').write_text(contents, encoding='utf-8')
    assert resolve_salesperson_userid(SimpleNamespace(runtime_dir=tmp_path), '新增销售员')['state'] == 'failed'
