"""上午任务的 Excel 日志，独立于价格提交和机器人私聊。"""
import base64
from datetime import date
import json
from pathlib import Path
import shutil
import subprocess

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from sqlalchemy import select

from app.models import Batch, Execution, Item
from app.services.erp_capture_store import write_verified
from app.services.store import stamp


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


def save_morning_log(config, day, *, factory=None, batch_id=None, capture=None):
    path = config.runtime_dir / 'morning-logs' / day.isoformat()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = path / _tk_filename(day)
    if filename.exists():
        return filename
    book = Workbook(write_only=True)
    if capture is not None:
        summary = [(day.isoformat(), '名单只读算价', capture['status'], capture['ready_prices'])]
        details = []
        for scope in capture['departments']:
            artifact = scope.get('artifact_dir')
            for name in ('pending-prices.json', 'unresolved.json') if artifact else ():
                for decision in json.loads((Path(artifact) / name).read_text(encoding='utf-8'))['decisions']:
                    key = decision['key']
                    details.append((scope.get('department'), scope.get('salesperson'), key.get('sku'),
                                    key.get('shop_id'), key.get('site'), '算价通过，未提交' if decision['ready']
                                    else decision['status'], decision.get('upload_price'), decision.get('reason')))
        _sheet(book, '采集范围', ('部门', '销售员', '运行状态', '算价通过', '采集编号', '排查编号'),
               [(s.get('department'), s.get('salesperson'), s['status'], s['ready_prices'], s.get('capture_id'),
                 s.get('error_id')) for s in capture['departments']])
        _sheet(book, '明细', ('部门', '销售员', 'SKU', '店铺ID', '站点', '状态', '建议上传价', '原因'), details)
    else:
        with factory() as session:
            batch = session.get(Batch, batch_id)
            if not batch or batch.mode != 'live' or batch.business_date != day.isoformat():
                raise ValueError('上午日志需要当天真实批次')
            summary = [(batch.business_date, '正式提价', batch.status, (batch.summary or {}).get('已核验', 0))]
            rows = session.execute(select(Item, Execution).outerjoin(Execution, Execution.item_id == Item.id)
                                   .where(Item.batch_id == batch_id).order_by(Item.id))
            details = [(i.key.get('sku'), i.key.get('shop_id'), i.key.get('site'), i.status,
                        (e.request or {}).get('upload_price') if e else None,
                        (e.evidence or {}).get('actual_price') if e else None, i.reason) for i, e in rows]
        _sheet(book, '明细', ('SKU', '店铺ID', '站点', '状态', '提交价', '回读实际价', '原因'), details)
    _sheet(book, '汇总', ('业务日期', '运行方式', '批次状态', '算价通过或已核验数'), summary)
    temporary = path / 'morning.tmp'
    book.save(temporary)
    temporary.replace(filename)
    return filename


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


def publish_morning_log(config, filename):
    if config.ding_tk_morning_log_folder:
        if config.ding_tk_morning_log_folder == config.ding_cancellation_log_folder:
            raise ValueError('TK亏损提价与作废订单日志必须使用各自的钉盘目录')
        if config.ding_drive_space_id:
            from app.adapters.dingtalk_drive import publish_drive_workbook
            publish_drive_workbook(config, filename, config.ding_tk_morning_log_folder,
                                   config.ding_tk_drive_parent_id)
        else:
            publish_workbook(config.ding_tk_morning_log_folder, filename)



def find_workbook(folder, directory, name):
    cursor = []
    seen = set()
    matches = []
    while True:
        listing = _dws(['drive', '+list', '--folder', folder, '--limit', '50', '--format', 'json',
                        *cursor], directory)
        matches.extend(row for row in listing['files'] if row['name'] == name)
        if not listing['hasMore']:
            break
        token = listing.get('nextCursor')
        if not token or token in seen:
            raise RuntimeError('钉盘目录分页未核对完整')
        seen.add(token)
        cursor = ['--cursor', token]
    if len(matches) > 1:
        raise RuntimeError('钉盘存在多份同名上午日志')
    return matches[0] if matches else None


def check_cancellation_log(config, day):
    folder = config.ding_cancellation_log_folder
    if not folder:
        return None
    if folder == config.ding_tk_morning_log_folder:
        raise ValueError('TK亏损提价与作废订单日志必须使用各自的钉盘目录')
    directory = config.runtime_dir / 'cancellation-logs' / day.isoformat()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f'{day.month}.{day.day}上午运行日志.xlsx'
    if config.ding_drive_space_id:
        from app.adapters.dingtalk_drive import find_drive_workbook
        entry = find_drive_workbook(config, folder, config.ding_cancellation_drive_parent_id, name)
    else:
        entry = find_workbook(folder, directory, name)
    return save_cancellation_lookup(day, directory, folder, name, entry)


def save_cancellation_lookup(day, directory, folder, name, entry):
    if entry and (entry.get('type') != 'FILE' or not entry.get('nodeId')):
        raise RuntimeError('同名作废日志没有可核实的文件节点')
    result = {'business_date': day.isoformat(), 'name': name, 'folder_id': folder,
              'status': 'found' if entry else 'pending', 'node_id': entry['nodeId'] if entry else None,
              'checked_at': stamp()}
    write_verified(directory / 'lookup.json', result)
    return result

def sync_cancellation_log(config, day):
    folder = config.ding_cancellation_log_folder
    if not folder:
        return None
    if folder == config.ding_tk_morning_log_folder:
        raise ValueError('TK亏损提价与作废订单日志必须使用各自的钉盘目录')
    filename = (config.runtime_dir / 'cancellation-logs' / day.isoformat()
                / f'{day.month}.{day.day}上午运行日志.xlsx')
    if not filename.is_file():
        return check_cancellation_log(config, day)

    # 作废订单由上游提供，工作簿结构和业务日期直接核对罗迪的格式。
    with filename.open('rb') as stream:
        workbook = load_workbook(stream, read_only=True, data_only=True)
        try:
            headers = {
                '成功清单': ('交易号', '店铺', '销售', '状态'),
                '失败清单': ('交易号', '店铺', '销售', '失败原因'),
                '汇总': ('运行时间', '总条数', '成功/处理中', '明确失败', '已剔除'),
            }
            if workbook.sheetnames != list(headers) or any(
                next(workbook[tab].values, ()) != columns for tab, columns in headers.items()
            ):
                raise ValueError('作废订单日志的工作表或列名与钉盘样表不一致')
            summary = workbook['汇总']
            if str(summary['A2'].value)[:10] != day.isoformat():
                raise ValueError('作废订单日志的运行日期与上午文件日期不一致')
        finally:
            workbook.close()
    if config.ding_drive_space_id:
        existing = check_cancellation_log(config, day)
        if existing['status'] == 'found':
            return existing
        from app.adapters.dingtalk_drive import publish_drive_workbook
        record = publish_drive_workbook(config, filename, folder, config.ding_cancellation_drive_parent_id)
        return save_cancellation_lookup(day, filename.parent, folder, filename.name,
                                        {'type': 'FILE', 'nodeId': record['node_id']})
    publish_workbook(folder, filename)
    return check_cancellation_log(config, day)


def publish_workbook(folder, filename):
    receipt = filename.with_suffix('.receipt.json')
    if receipt.exists():
        if json.loads(receipt.read_text(encoding='utf-8'))['folder_id'] != folder:
            raise ValueError('现有上传回执属于其他目录')
        return
    if find_workbook(folder, filename.parent, filename.name):
        raise RuntimeError('钉盘已存在同名上午日志')
    # 有明确的上午日志发布授权，上传只创建文件，绝不覆盖旧节点。
    result = _dws(['drive', '+upload', '--folder', folder, '--file', filename.name,
                   '--format', 'json', '--yes'], filename.parent)
    node = result.get('nodeId')
    if not node:
        raise RuntimeError('钉盘未返回新文件节点，不自动重传')
    write_verified(receipt, {'node_id': node, 'folder_id': folder, 'name': filename.name, 'published_at': stamp()})



if __name__ == '__main__':
    import argparse
    from app.config import get_settings

    parser = argparse.ArgumentParser(description='发布TK早上运行日志或查询罗迪的作废订单日志')
    parser.add_argument('day', type=date.fromisoformat)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-cancellation', action='store_true')
    mode.add_argument('--publish-cancellation', action='store_true')
    args = parser.parse_args()
    day = args.day
    config = get_settings()
    if args.check_cancellation or args.publish_cancellation:
        if not config.ding_cancellation_log_folder:
            parser.error('需要配置作废订单日志目录')
        handler = sync_cancellation_log if args.publish_cancellation else check_cancellation_log
        print(json.dumps(handler(config, day), ensure_ascii=False))
        raise SystemExit(0)
    filename = config.runtime_dir / 'morning-logs' / day.isoformat() / _tk_filename(day)
    if not config.ding_tk_morning_log_folder or not filename.is_file():
        parser.error('需要配置钉盘目标目录并存在当日工作簿')
    publish_morning_log(config, filename)
