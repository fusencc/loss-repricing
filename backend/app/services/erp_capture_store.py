"""ERP来源采集文件；不写业务快照、执行意图或调价数据库。

路径由当前服务的runtime_dir决定。每轮独立文件，金额字符串和空值不转换；
写入后回读整个结构，确认列表、明细、失败证据均一致才返回保存成功。

批量订单表在groups[].rows；历史归档可能另有details明细证据。find_order仅查已保存主记录，
不请求ERP、不扫描原始XLSX，也不把未下载订单或空details解释为无业务数据。
storage_verified仅证明本次写入回读一致，不证明来源完整、公式正确或价格生效。
"""
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import gzip
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from app.observability import event
from app.services.erp_clean_capture import save_clean_capture


CAPTURE_ID = re.compile(r'erp-capture-[0-9]{8}T[0-9]{12}Z-[a-f0-9]{32}')


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_verified(path, data):
    path = Path(path)
    write_json(path, data)
    restored = json.loads(path.read_text(encoding='utf-8'))
    if restored != data:
        path.unlink()
        raise OSError('ERP采集文件回读不一致，未确认保存成功')


BULK_DIRECTORY = re.compile(r'erp-bulk-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}')


def _archive_path(root, folder):
    if not isinstance(folder, str) or not BULK_DIRECTORY.fullmatch(folder):
        raise ValueError('ERP批量采集目录格式无效')
    path = root / folder / 'result.json.gz'
    if path.parent.resolve() != root.resolve() / folder or path.is_symlink():
        raise ValueError('ERP批量采集路径超出范围')
    return path


def _read_archive(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def _save_bulk_archive(root, manifest, stored):
    checkpoint = Path(stored['result_file'])
    archive = _archive_path(root, checkpoint.parent.name)
    if checkpoint.resolve() != archive.with_name('result.json').resolve():
        raise ValueError('ERP批量采集检查点路径无效')
    stored['result_file'] = str(archive)
    temporary = archive.with_name(f'{uuid4().hex}.tmp')
    try:
        with gzip.open(temporary, 'xt', encoding='utf-8', compresslevel=3) as stream:
            json.dump(stored, stream, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if _read_archive(temporary) != stored:
            raise OSError('ERP压缩采集文件回读不一致')
        os.link(temporary, archive)
        try:
            write_verified(manifest, {'capture_id': stored['capture_id'], 'bulk_archive': archive.parent.name})
        except (OSError, ValueError, TypeError):
            archive.unlink()
            raise
    finally:
        temporary.unlink(missing_ok=True)
    try:
        checkpoint.unlink(missing_ok=True)
    except OSError as error:
        event('capture.failed', exception=error, source='erp', synthetic=False,
              capture_id=stored['capture_id'], stage='save', error_code='storage_failed')


def rebuild_clean_capture(runtime_dir, capture_id):
    return save_clean_capture(runtime_dir, load_capture(runtime_dir, capture_id))


def save_capture(runtime_dir, result):
    """登记本项目批量采集检查点或桌面批量结果。"""
    if result.get('acquisition_mode') != 'bulk_download':
        raise ValueError('只接受 ERP 批量采集结果')
    capture_id = 'erp-capture-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid4().hex
    stored = dict(result, capture_id=capture_id)
    try:
        root = Path(runtime_dir) / 'source-captures'
        manifest = root / (capture_id + '.json')
        if result.get('result_file'):
            _save_bulk_archive(root, manifest, stored)
        else:
            # 桌面批量文件由操作者登记，原件位于桌面输出目录。
            write_verified(manifest, stored)
    except (OSError, ValueError, TypeError) as error:
        error_id = event('capture.failed', exception=error, source='erp', synthetic=False,
                         capture_id=capture_id, stage='save', error_code='storage_failed')
        if error_id:
            # 同一保存失败交给API/CLI转换时沿用编号；元数据失败不得改变原始异常。
            try:
                error._erp_log_error_id = error_id
            except Exception:
                pass
        raise
    event('capture.saved', source='erp', synthetic=False, capture_id=capture_id,
          stage='save', complete=bool(result.get('capture_complete', False)))
    response = dict(stored, storage_verified=True)
    try:
        counts = save_clean_capture(runtime_dir, stored)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        response['cleaning_status'] = 'failed'
        event('capture.clean_failed', exception=error, source='erp', synthetic=False,
              capture_id=capture_id, stage='save', error_code='storage_failed')
    else:
        response['cleaning_status'] = 'saved'
        event('capture.cleaned', source='erp', synthetic=False, capture_id=capture_id,
              stage='save', count=counts['orders'])
    return response


def _read_manifest(path):
    return json.loads(path.read_text(encoding='utf-8'))


@lru_cache(maxsize=2)
def _decode_for_order_lookup(reader, path, file_version):
    # 两个槽位容纳压缩归档及其清单。整批读取与清洗重放仍直接读原件。
    return reader(path)


def _read_for_order_lookup(reader, path):
    path = path.absolute()
    stat = path.stat()
    version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return _decode_for_order_lookup(reader, path, version)


def _load_capture(runtime_dir, capture_id, read_manifest, read_archive):
    if not CAPTURE_ID.fullmatch(capture_id):
        raise ValueError('采集编号格式无效')
    path = Path(runtime_dir) / 'source-captures' / (capture_id + '.json')
    data = read_manifest(path)
    if not isinstance(data, dict) or data.get('capture_id') != capture_id:
        raise ValueError('ERP采集文件结构或编号无效')
    if 'bulk_archive' in data:
        try:
            data = read_archive(_archive_path(path.parent, data['bulk_archive']))
        except EOFError as error:
            raise OSError('ERP压缩采集文件不完整') from error
        if not isinstance(data, dict) or data.get('capture_id') != capture_id:
            raise ValueError('ERP批量归档编号无效')
    return data


def load_capture(runtime_dir, capture_id):
    return _load_capture(runtime_dir, capture_id, _read_manifest, _read_archive)


def find_saved_order(runtime_dir, capture_id, orderid):
    capture = _load_capture(
        runtime_dir, capture_id,
        lambda path: _read_for_order_lookup(_read_manifest, path),
        lambda path: _read_for_order_lookup(_read_archive, path),
    )
    # 返回订单的小结构，避免调用方修改缓存的原始财务行。
    return deepcopy(find_order(capture, orderid))


def _finance_gap(group, orderid, rows, batches, *, sample_only):
    if len(rows) > 1:
        return 'duplicate_finance_rows', '该来源返回了同订单的重复财务行，已保留原件供核对'
    if orderid in group.get('missing_order_ids', []):
        return 'not_returned_by_bulk_export', '候选名单中存在该订单，完整订单批量导出未返回财务记录'
    if rows:
        return None
    statuses = {batch['status'] for batch in batches}
    if 'failed' in statuses:
        return 'finance_export_failed', '该订单所属财务导出子批失败'
    if statuses == {'not_attempted'}:
        return 'finance_export_not_attempted', '本轮未发起该订单所属的财务导出子批'
    if sample_only and not batches and group.get('list_complete'):
        return 'finance_not_selected', '该订单在候选名单中，但不在本次试抓财务范围内'
    return None


def find_order(capture, orderid):
    groups = []
    for group in capture['groups']:
        rows = [row for row in group['rows'] if row.get('orderid') == orderid]
        # 批量源可能只在亏损池保留历史订单，财务导出未返回；应能按单号查看缺失证据。
        loss_rows = [row for row in group.get('loss_rows', []) if row.get('订单编号') == orderid]
        if rows or loss_rows or orderid in group.get('candidate_order_ids', []):
            entry = {'kind': group['kind'], 'status': group['status'], 'rows': rows}
            if 'source' in group:
                entry['source'] = group['source']
            finance_batches = []
            for batch in group.get('batches', []):
                if orderid not in batch.get('order_ids', []):
                    continue
                number = batch['number']
                filename = f"{group['source']}-order-finance-{number:03d}.xlsx"
                # 用归档里的子批名单及采集器文件名定位原件，不扫描 XLSX 或猜行号。
                originals = [item for item in group.get('files', []) if Path(item['file']).name == filename]
                finance_batches.append({'number': number, 'status': batch['status'], 'originals': originals})
            if finance_batches:
                entry['finance_batches'] = finance_batches
            if loss_rows:
                entry['loss_rows'] = loss_rows
            gap = _finance_gap(group, orderid, rows, finance_batches, sample_only=capture.get('sample_only', False))
            if gap:
                entry.update(finance_error_kind=gap[0], finance_error=gap[1])
            groups.append(entry)
    detail = next((d for d in capture['details'] if d.get('orderid') == orderid), None)
    if not groups and detail is None:
        raise KeyError('本轮采集未找到该订单')
    return {'capture_id': capture['capture_id'], 'queried_at': capture['queried_at'],
            'orderid': orderid, 'groups': groups, 'detail': detail}
