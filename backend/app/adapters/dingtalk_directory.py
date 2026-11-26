"""从本项目人员目录按 ERP 销售员姓名定位钉钉收件人。"""
import json
from pathlib import Path


def resolve_salesperson_userid(config, name):
    try:
        directory = json.loads((Path(config.runtime_dir) / 'dingtalk_users.json').read_text(encoding='utf-8'))
        users = directory['users']
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'failed', 'error': '钉钉人员目录不可用，未发送'}
    if not isinstance(users, list):
        return {'state': 'failed', 'error': '钉钉人员目录不可用，未发送'}
    name = str(name or '').strip()
    active = [row for row in users if isinstance(row, dict) and row.get('active') is True]
    matches = [row for row in active if str(row.get('name', '')).strip() == name] if name else []
    userids = {row.get('user_id') for row in matches if isinstance(row.get('user_id'), str) and row['user_id'].strip()}
    # 同名多人、缺失 ID 或多个姓名共用 ID 时不猜测收件人。
    if not matches or len(userids) != 1 or any(not isinstance(row.get('user_id'), str) or not row['user_id'].strip() for row in matches):
        return {'state': 'failed', 'error': '销售员姓名未匹配唯一启用的钉钉人员，未发送'}
    userid = next(iter(userids))
    if any(row.get('user_id') == userid and str(row.get('name', '')).strip() != name for row in active):
        return {'state': 'failed', 'error': '钉钉 User ID 对应多个姓名，未发送'}
    return {'userid': userid}
