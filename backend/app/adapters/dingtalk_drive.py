"""使用预算归档已采用的企业存储接口，发布亏损提价项目自己的日志。"""
from contextlib import contextmanager
from hashlib import sha256
import json
from urllib.parse import urlsplit

from app.adapters.dingtalk import DingTalkClient
from app.services.erp_capture_store import write_json
from app.services.store import stamp


@contextmanager
def drive_folder(config, folder, parent):
    space = config.ding_drive_space_id
    operator = config.ding_drive_operator_userid
    if not (space.isdecimal() and parent.isdecimal() and parent != '0' and operator):
        raise ValueError('缺少钉盘目录或企业应用操作人配置')
    ding = DingTalkClient(config)
    try:
        user = ding._post('topapi/v2/user/get', {'userid': operator})
        union_id = user.get('result', {}).get('unionid')
        if user.get('errcode') != 0 or not union_id:
            raise RuntimeError('钉盘操作人无法通过企业应用解析')
        token = ding._token()

        def request(method, path, payload=None, params=None):
            response = ding.http.request(
                method, 'https://api.dingtalk.com' + path, params={'unionId': union_id, **(params or {})},
                json=payload, headers={'x-acs-dingtalk-access-token': token}, timeout=45)
            if not response.is_success:
                raise RuntimeError(f'钉盘企业存储接口返回 HTTP {response.status_code}')
            return response.json()

        def inspect(dentry_id):
            return request('POST', f'/v1.0/storage/spaces/{space}/dentries/{dentry_id}/query', {})['dentry']

        target = inspect(parent)
        if (target.get('type') != 'FOLDER' or str(target.get('spaceId')) != space
                or str(target.get('id')) != parent or target.get('uuid') != folder):
            raise RuntimeError('钉盘目录与部署配置不一致')
        yield ding, request, inspect
    finally:
        ding.close()


def find_drive_workbook(space, parent, name, request, inspect):
    path = f'/v1.0/storage/spaces/{space}/dentries'
    matches, cursors = [], set()
    params = {'parentId': parent, 'maxResults': 50}
    while True:
        page = request('GET', path, params=params)
        matches.extend(item for item in page['dentries'] if item.get('name') == name)
        cursor = page.get('nextToken')
        if not cursor:
            break
        if cursor in cursors:
            raise RuntimeError('钉盘目录分页未核对完整')
        cursors.add(cursor)
        params['nextToken'] = cursor
    if len(matches) > 1:
        raise RuntimeError('钉盘存在多份同名早上日志')
    if matches:
        row = matches[0]
        if not (row.get('uuid') and row.get('type')):
            row = inspect(row['id'])
        if (row.get('name') != name or str(row.get('parentId', parent)) != parent
                or str(row.get('spaceId', space)) != space):
            raise RuntimeError('钉盘文件与目标目录不一致')
        return {'type': row['type'], 'nodeId': row['uuid']}
    return None


def publish_drive_workbook(config, filename, folder, parent):
    space = config.ding_drive_space_id
    receipt = filename.with_suffix('.receipt.json')
    raw = filename.read_bytes()
    digest = sha256(raw).hexdigest()
    record = None
    if receipt.exists():
        record = json.loads(receipt.read_text(encoding='utf-8'))
        if record['folder_id'] != folder:
            raise ValueError('现有上传回执属于其他目录')
    intent = filename.with_suffix('.publish-intent.json')
    if intent.exists():
        pending = json.loads(intent.read_text(encoding='utf-8'))
        if record and pending['folder_id'] == folder and pending['sha256'] == record.get('sha256'):
            # 回执已核验落盘但清理意图前退出，不将已确认的归档误判成未知提交。
            intent.unlink()
        else:
            raise RuntimeError('上次钉盘提交结果待核验，禁止自动重传')
    if record and record.get('sha256') == digest:
        return record
    with drive_folder(config, folder, parent) as (ding, request, inspect):
        existing = find_drive_workbook(space, parent, filename.name, request, inspect)
        if existing and (existing['type'] != 'FILE' or (record and existing['nodeId'] != record['node_id'])):
            raise RuntimeError('同名日志不是本次归档文件')
        base = f'/v2.0/storage/spaces/files/{folder}'
        info = request('POST', base + '/uploadInfos/query', {
            'protocol': 'HEADER_SIGNATURE', 'option': {
                'storageDriver': 'DINGTALK', 'preferIntranet': False,
                'preCheckParam': {'name': filename.name, 'size': len(raw)},
            },
        })
        signature = info['headerSignatureInfo']
        url, = signature['resourceUrls']
        destination = urlsplit(url)
        if destination.scheme != 'https' or not (destination.hostname or '').endswith(('.dingtalk.com', '.aliyuncs.com')):
            raise RuntimeError('钉盘上传地址不属于已确认的存储服务')
        upload = ding.http.put(url, headers=signature['headers'], content=raw, timeout=120)
        if not upload.is_success:
            raise RuntimeError(f'钉盘文件上传返回 HTTP {upload.status_code}')
        write_json(intent, {'folder_id': folder, 'name': filename.name, 'sha256': digest, 'submitted_at': stamp()})
        committed = request('POST', base + '/commit', {
            'uploadKey': info['uploadKey'], 'name': filename.name, 'option': {
                # 两个已配置目录中各自固定的日期日志随核验结果原位更新；新建仍禁止重名。
                'size': len(raw), 'conflictStrategy': 'OVERWRITE' if existing else 'RETURN_ERROR_IF_EXISTS',
                'convertToOnlineDoc': False,
                'appProperties': [{'name': 'archive_sha256', 'value': digest, 'visibility': 'PRIVATE'}],
            },
        })['dentry']
        row = committed
        if not all(row.get(key) for key in ('uuid', 'name', 'parentId')):
            row = inspect(committed['id'])
        if (not row.get('uuid') or row.get('uuid') != committed.get('uuid', row['uuid'])
                or row.get('name') != filename.name or str(row.get('parentId')) != parent
                or str(row.get('spaceId', space)) != space
                or (existing and row.get('uuid') != existing['nodeId'])):
            raise RuntimeError('钉盘文件回执与本次发布不一致')
        record = {'node_id': row['uuid'], 'folder_id': folder, 'name': filename.name,
                  'sha256': digest, 'published_at': stamp()}
        write_json(receipt, record)
        intent.unlink()
        return record
