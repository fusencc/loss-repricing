"""使用预算归档已采用的企业存储接口，发布亏损提价项目自己的日志。"""
from hashlib import sha256
import json
from urllib.parse import urlsplit

from app.adapters.dingtalk import DingTalkClient
from app.services.erp_capture_store import write_verified
from app.services.store import stamp


def publish_tk_workbook(config, filename):
    folder = config.ding_tk_morning_log_folder
    space = config.ding_tk_drive_space_id
    parent = config.ding_tk_drive_parent_id
    operator = config.ding_tk_drive_operator_userid
    if not (space.isdecimal() and parent.isdecimal() and parent != '0' and operator):
        raise ValueError('缺少钉盘目录或企业应用操作人配置')
    receipt = filename.with_suffix('.receipt.json')
    if receipt.exists():
        if json.loads(receipt.read_text(encoding='utf-8'))['folder_id'] != folder:
            raise ValueError('现有上传回执属于其他目录')
        return

    raw = filename.read_bytes()
    intent = filename.with_suffix('.publish-intent.json')
    if intent.exists():
        raise RuntimeError('上次钉盘提交结果待核验，禁止自动重传')
    ding = DingTalkClient(config)
    try:
        user = ding._post('topapi/v2/user/get', {'userid': operator})
        union_id = user.get('result', {}).get('unionid')
        if user.get('errcode') != 0 or not union_id:
            raise RuntimeError('钉盘操作人无法通过企业应用解析')
        token = ding._token()

        def request(method, path, payload):
            response = ding.http.request(
                method, 'https://api.dingtalk.com' + path, params={'unionId': union_id},
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
        digest = sha256(raw).hexdigest()
        write_verified(intent, {'folder_id': folder, 'name': filename.name, 'sha256': digest, 'submitted_at': stamp()})
        committed = request('POST', base + '/commit', {
            'uploadKey': info['uploadKey'], 'name': filename.name, 'option': {
                'size': len(raw), 'conflictStrategy': 'RETURN_ERROR_IF_EXISTS', 'convertToOnlineDoc': False,
                'appProperties': [{'name': 'archive_sha256', 'value': digest, 'visibility': 'PRIVATE'}],
            },
        })['dentry']
        row = inspect(committed['id'])
        if (row.get('uuid') != committed['uuid'] or row.get('name') != filename.name or row.get('size') != len(raw)
                or str(row.get('spaceId')) != space or str(row.get('parentId')) != parent):
            raise RuntimeError('钉盘文件回执与本次发布不一致')
        write_verified(receipt, {'node_id': row['uuid'], 'folder_id': folder, 'name': filename.name,
                                 'sha256': digest, 'published_at': stamp()})
    finally:
        ding.close()
