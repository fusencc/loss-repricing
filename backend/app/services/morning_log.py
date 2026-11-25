"""上午任务的 Excel 日志，独立于价格提交和机器人私聊。"""
import base64
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from sqlalchemy import select

from app.models import Batch, Execution, Item, Ownership, UserAccount
from app.services.erp_capture_store import write_verified
from app.services.store import stamp, BEIJING


DETAIL_HEADERS = ('处理时间', '销售员', 'SKU', '商品ID', '产品ID', '店铺ID', '站点', '仓配',
                  '状态', '原实际营销价', '目标实际营销价', '上传价', '币种', '处理说明')


def _sheet(workbook, title, headers, rows):
    sheet = workbook.create_sheet(title)
    for row in (headers, *rows):
        # ERP名称、SKU及编号均为文本，避免Excel公式执行和长编号截断。
        cells = []
        for value in row:
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                cell.data_type = 's'
            cells.append(cell)
        sheet.append(cells)


def _tk_filename(day):
    return f'{day.month}.{day.day}早上运行日志.xlsx'


def _records(factory, day):
    from app.services.records import (combine_pricing_process, public_record, record_department,
                                      record_seller, KEY_FIELDS)
    with factory() as session:
        ownerships = {row.scope_key: row for row in session.scalars(select(Ownership))}
        names = dict(session.execute(select(UserAccount.id, UserAccount.display_name)).all())
        rows = session.execute(select(Item, Batch, Execution).join(Batch, Batch.id == Item.batch_id)
            .outerjoin(Execution, Execution.item_id == Item.id)
            .where(Batch.mode == 'live', Batch.business_date == day.isoformat())
            .order_by(Item.updated_at.desc(), Item.id.desc()))
        projected, sources = [], {}
        for item, batch, execution in rows:
            record = public_record(item, batch, execution, record_department(item, batch),
                                   record_seller(item, batch, ownerships, names))
            if record['synthetic']:
                continue
            projected.append(record)
            # 交易号必须追溯到本次执行的原请求，不借后来的重算结果补成功订单。
            request = execution.request if execution else None
            if request and all((request.get('key') or {}).get(k) == item.key.get(k) for k in KEY_FIELDS):
                sources[item.id] = request.get('trace') or {}
        return combine_pricing_process(projected), sources


def _save_book(config, day, directory, sheets):
    path = config.runtime_dir / directory / day.isoformat()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = path / _tk_filename(day)
    intent = filename.with_suffix('.publish-intent.json')
    if intent.exists():
        pending = json.loads(intent.read_text(encoding='utf-8'))
        receipt = filename.with_suffix('.receipt.json')
        confirmed = json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}
        if (confirmed.get('sha256') == pending.get('sha256')
                and confirmed.get('folder_id') == pending.get('folder_id')):
            intent.unlink()
        else:
            # 未知提交仍需核对原字节；不能先生成新版本覆盖待核验的本地工作簿。
            raise RuntimeError('上次钉盘提交结果待核验，保留原工作簿')
    content = sha256(json.dumps(sheets, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    manifest = filename.with_suffix('.content.json')
    if filename.exists() and manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) == content:
        return filename
    book = Workbook(write_only=True)
    for title, headers, rows in sheets:
        _sheet(book, title, headers, rows)
    temporary = path / 'morning.tmp'
    book.save(temporary)
    temporary.replace(filename)
    write_verified(manifest, content)
    return filename


def save_morning_log(config, day, *, factory):
    records, _ = _records(factory, day)
    details = []
    for record in records:
        key, decision = record['key'], record.get('decision') or {}
        when = datetime.fromisoformat(record['updated_at']).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        details.append((when, record['seller']['name'], key['sku'], key['item_id'], key['product_id'],
                        key['shop_id'], key['site'], key['warehouse'], record['status'],
                        decision.get('current_actual'), decision.get('target_actual'), decision.get('upload_price'),
                        (decision.get('trace') or {}).get('station_currency'), record['reason']))
    return _save_book(config, day, 'morning-logs', [('明细', DETAIL_HEADERS, details)])


def _trade_catalog(config, capture_id):
    from app.services.erp_capture_store import load_capture
    capture = load_capture(config.runtime_dir, capture_id)
    catalog = {}
    for group in capture['groups']:
        for file in group.get('files', []):
            if '-order-finance-' not in Path(file['file']).name:
                continue
            # 交易号来自原始订单列表导出的同名列；ERP订单编号可能拼接店铺编码，禁止截断猜测。
            book = load_workbook(file['file'], read_only=True, data_only=True)
            try:
                for sheet in book:
                    rows = sheet.iter_rows(values_only=True)
                    headers = next(rows, ())
                    columns = ('订单编号', '交易号', '店铺', '销售员')
                    if not all(column in headers for column in columns):
                        continue
                    indexes = [headers.index(column) for column in columns]
                    for row in rows:
                        order, trade, shop, seller = (row[index] for index in indexes)
                        # 长交易号只有源端文本可保证精度；缺失或数值类型不冒充可过滤的编号。
                        if all(isinstance(value, str) and value.strip() for value in (order, trade, shop, seller)):
                            catalog.setdefault(order, set()).add((trade, shop, seller))
            finally:
                book.close()
    return catalog


def save_success_log(config, day, *, factory):
    from app.services.records import KEY_FIELDS
    records, sources = _records(factory, day)
    catalogs, orders, times = {}, set(), []
    for record in records:
        execution = record.get('execution') or {}
        if record['status'] != '已核验' or execution.get('state') != 'verified':
            continue
        trace = sources.get(record['id']) or {}
        capture_id = (trace.get('source_versions') or {}).get('erp_capture_id')
        if not capture_id or not trace.get('source_rows'):
            raise ValueError('提价成功记录缺少原执行订单来源，不能生成交易号过滤清单')
        if capture_id not in catalogs:
            catalogs[capture_id] = _trade_catalog(config, capture_id)
        for row in trace['source_rows']:
            if row.get('row_id') in (trace.get('excluded_source_rows') or {}):
                continue
            if row.get('key') and any(row['key'].get(k) != record['key'].get(k) for k in KEY_FIELDS):
                raise ValueError('订单来源与已核验提价商品不一致')
            matches = catalogs[capture_id].get(row.get('order_id'), set())
            if len(matches) != 1:
                raise ValueError('已核验商品的订单交易号缺失或不唯一')
            trade, shop, seller = next(iter(matches))
            if seller != record['seller']['name'] or (record['shop_name'] and shop != record['shop_name']):
                raise ValueError('订单交易号的店铺或销售员与提价执行不一致')
            orders.add((trade, shop, seller, '提价成功'))
        times.append(record['updated_at'])
    when = (datetime.fromisoformat(max(times)).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
            if times else None)
    return _save_book(config, day, 'success-logs', [
        ('成功清单', ('交易号', '店铺', '销售', '状态'), sorted(orders)),
        ('失败清单', ('交易号', '店铺', '销售', '失败原因'), []),
        ('汇总', ('运行时间', '总条数', '成功/处理中', '明确失败', '已剔除'),
         [(when, len(orders), len(orders), 0, 0)]),
    ])


def _dws(args, directory):
    if command := shutil.which('dws'):
        cmd = [command, *args]
    else:
        # Windows 上已有登录的 dws，WSL 通过同一 Windows 用户调用并在临时目录暂存文件。
        directory_win = subprocess.check_output(['wslpath', '-w', str(directory)], text=True).strip()
        payload = base64.b64encode(json.dumps([directory_win, args]).encode()).decode()
        ps = """
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')) | ConvertFrom-Json
$arguments = @($payload[1])
$cli = (Get-Command dws.cmd -ErrorAction Stop).Source
$staging = Join-Path $env:TEMP ('loss-repricing-morning-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $staging -ErrorAction Stop | Out-Null
Push-Location $staging
try {
    $index = [Array]::IndexOf($arguments, '--file')
    if ($index -ge 0) {
        $name = $arguments[$index + 1]
        Copy-Item -LiteralPath (Join-Path $payload[0] $name) -Destination $staging -ErrorAction Stop
    }
    & $cli @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
    if ($index -ge 0 -and $name) {
        $stagedFile = Join-Path $staging $name
        if (Test-Path -LiteralPath $stagedFile) { Remove-Item -LiteralPath $stagedFile }
    }
    Remove-Item -LiteralPath $staging
}
""".replace('PAYLOAD', payload)
        powershell = shutil.which('powershell.exe') or '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
        cmd = [powershell, '-NoProfile', '-NonInteractive', '-EncodedCommand',
               base64.b64encode(ps.encode('utf-16-le')).decode()]
    result = subprocess.run(cmd, cwd=directory, capture_output=True, encoding='utf-8', timeout=90)
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('钉盘未返回可核验的结果') from None
    if result.returncode or not response.get('ok'):
        raise RuntimeError('钉盘日志操作未成功，请核对目录权限及登录状态')
    return response['data']


def publish_morning_log(config, filename, *, success=False):
    if config.ding_tk_morning_log_folder == config.ding_cancellation_log_folder and config.ding_tk_morning_log_folder:
        raise ValueError('完整提价明细与成功订单清单必须使用各自的钉盘目录')
    folder = config.ding_cancellation_log_folder if success else config.ding_tk_morning_log_folder
    parent = config.ding_cancellation_drive_parent_id if success else config.ding_tk_drive_parent_id
    if not folder:
        return
    if config.ding_drive_space_id:
        from app.adapters.dingtalk_drive import publish_drive_workbook
        return publish_drive_workbook(config, filename, folder, parent)
    return publish_workbook(folder, filename)


def find_workbook(folder, directory, name):
    listing = _dws(['drive', '+list', '--folder', folder, '--page-all', '--max-pages', '20',
                    '--max-items', '500', '--format', 'json'], directory)
    if listing.get('hasMore'):
        raise RuntimeError('钉盘目录分页未核对完整')
    matches = [row for row in listing['files'] if row.get('name') == name]
    if len(matches) > 1:
        raise RuntimeError('钉盘存在多份同名早上日志')
    if matches and matches[0].get('type') != 'FILE':
        raise ValueError('早上日志同名节点不是普通文件')
    return matches[0] if matches else None


def publish_workbook(folder, filename):
    receipt = filename.with_suffix('.receipt.json')
    digest = sha256(filename.read_bytes()).hexdigest()
    record = None
    if receipt.exists():
        record = json.loads(receipt.read_text(encoding='utf-8'))
        if record['folder_id'] != folder:
            raise ValueError('现有上传回执属于其他目录')
        if record.get('sha256') == digest:
            return record
    intent = filename.with_suffix('.publish-intent.json')
    if intent.exists():
        raise RuntimeError('上次钉盘提交结果待核验，禁止自动重传')
    existing = find_workbook(folder, filename.parent, filename.name)
    if existing and record and existing['nodeId'] != record['node_id']:
        raise RuntimeError('同名日志不是本次归档文件')
    target = ['--node', existing['nodeId']] if existing else ['--folder', folder]
    write_verified(intent, {'folder_id': folder, 'name': filename.name, 'sha256': digest})
    result = _dws(['drive', '+upload', *target, '--file', filename.name, '--format', 'json', '--yes'], filename.parent)
    node = result.get('nodeId')
    if not node or (existing and node != existing['nodeId']):
        raise RuntimeError('钉盘未返回一致的文件节点，不自动重传')
    record = {'node_id': node, 'folder_id': folder, 'name': filename.name,
              'sha256': digest, 'published_at': stamp()}
    write_verified(receipt, record)
    intent.unlink()
    return record


def main(argv=None):
    import argparse
    from app.config import get_settings
    from app.db import create_database

    parser = argparse.ArgumentParser(description='生成并发布两处提价日志：完整明细和成功订单清单')
    parser.add_argument('day', type=date.fromisoformat)
    args = parser.parse_args(argv)
    config = get_settings()
    engine, factory = create_database(config.database_url)
    failures = []
    try:
        for success, save in ((False, save_morning_log), (True, save_success_log)):
            try:
                publish_morning_log(config, save(config, args.day, factory=factory), success=success)
            except Exception as exc:
                failures.append(f"{'成功清单' if success else '完整明细'}写入失败（{type(exc).__name__}）")
    finally:
        engine.dispose()
    if failures:
        parser.exit(1, '；'.join(failures) + '\n')


if __name__ == '__main__':
    main()
