"""独立桌面程序入口，构建时与已测试的批量核心合为一个可读Python文件。"""
import argparse
import getpass
import sys
from types import SimpleNamespace
import os
import stat
import math
from threading import RLock


_DESKTOP_EVENTS = {
    'erp.started': 'ERP读取开始', 'erp.stage': 'ERP读取阶段',
    'erp.completed': 'ERP读取结束', 'erp.failed': 'ERP读取失败',
    'capture.saved': '采集证据已保存', 'capture.failed': '采集证据保存失败',
}
_DESKTOP_STAGES = {'capture', 'auth', 'pool', 'export', 'poll', 'download',
                   'complete', 'save'}


@contextmanager
def _desktop_log_directory(folder):
    """逐级拒绝符号链接；支持dirfd的平台在打开的目录句柄内操作日志。"""
    folder = Path(os.path.abspath(folder))
    if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        fd = os.open(folder.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in folder.parts[1:]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)
    else:
        # Windows原生桌面不依赖dirfd；每次写入重新拒绝可见链接，文件用独占创建及身份核对。
        for parent in (*reversed(folder.parents), folder):
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise OSError('unsafe log directory')
            parent.mkdir(exist_ok=True, mode=0o700)
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise OSError('unsafe log directory')
        yield None


def _desktop_logger(output):
    """日志故障仅提示静态说明；不调用logging默认错误处理，避免输出原始异常。"""
    folder = Path(output) / 'logs'
    operation_id = uuid4().hex
    name = 'capture-' + str(os.getpid()) + '-' + uuid4().hex + '.jsonl'
    identity = None
    lock = RLock()
    warned = False

    def fallback():
        nonlocal warned
        if not warned:
            warned = True
            try:
                sys.stderr.write('桌面技术日志暂不可用；读取及证据保存流程保持原规则。\n')
            except Exception:
                pass

    def write(encoded):
        nonlocal identity
        with lock, _desktop_log_directory(folder) as directory:
            def location(filename):
                return filename if directory is not None else str(folder / filename)

            def options():
                return {'dir_fd': directory} if directory is not None else {}

            def checked_stat(filename):
                try:
                    info = os.stat(location(filename), follow_symlinks=False, **options())
                except FileNotFoundError:
                    return None
                if not stat.S_ISREG(info.st_mode):
                    raise OSError('unsafe log file')
                return info

            def open_file():
                flags = os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
                if identity is None:
                    flags |= os.O_CREAT | os.O_EXCL
                descriptor = os.open(location(name), flags, 0o600, **options())
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or (identity is not None and (info.st_dev, info.st_ino) != identity):
                    os.close(descriptor)
                    raise OSError('unsafe log identity')
                return descriptor, info

            checked_stat(name)
            descriptor, info = open_file()
            identity = (info.st_dev, info.st_ino)
            try:
                if info.st_size and info.st_size + len(encoded) > 10 * 1024 * 1024:
                    os.close(descriptor)
                    descriptor = None
                    for index in range(3, 0, -1):
                        checked_stat(name + '.' + str(index))
                    for index in range(2, 0, -1):
                        source = name + '.' + str(index)
                        if checked_stat(source) is not None:
                            if directory is None:
                                os.replace(location(source), location(name + '.' + str(index + 1)))
                            else:
                                os.replace(source, name + '.' + str(index + 1), src_dir_fd=directory, dst_dir_fd=directory)
                    if directory is None:
                        os.replace(location(name), location(name + '.1'))
                    else:
                        os.replace(name, name + '.1', src_dir_fd=directory, dst_dir_fd=directory)
                    identity = None
                    descriptor, info = open_file()
                    identity = (info.st_dev, info.st_ino)
                # 使用已核对的文件描述符写入，磁盘满/权限失败均由安全回退处理。
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError('log write incomplete')
                    remaining = remaining[written:]
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def safe_event(name, exception=None, **fields):
        error_id = uuid4().hex if exception is not None else None
        try:
            if name not in _DESKTOP_EVENTS:
                return None
            record = {'timestamp': datetime.now(timezone.utc).isoformat(), 'role': 'capture',
                      'event': name, 'message': _DESKTOP_EVENTS[name], 'source': 'erp',
                      'synthetic': False, 'operation_id': operation_id}
            if fields.get('stage') in _DESKTOP_STAGES:
                record['stage'] = fields['stage']
            for key in ('count', 'total', 'duration_ms'):
                value = fields.get(key)
                expected = type(value) is int if key != 'duration_ms' else type(value) in (int, float)
                if expected and math.isfinite(value) and 0 <= value <= 10**12:
                    record[key] = value
            for key in ('complete', 'limited'):
                if type(fields.get(key)) is bool:
                    record[key] = fields[key]
            if fields.get('error_code') in ('rate_limited', 'source_unavailable', 'storage_failed'):
                record['error_code'] = fields['error_code']
            if error_id:
                record['error_id'] = error_id
                frames, trace = [], exception.__traceback__
                while trace is not None:
                    filename = Path(trace.tb_frame.f_code.co_filename).name
                    function = trace.tb_frame.f_code.co_name
                    if re.fullmatch(r'[A-Za-z0-9_.-]+\.py', filename) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_<>]*', function):
                        frames.append({'file': filename, 'function': function, 'line': trace.tb_lineno})
                    trace = trace.tb_next
                record['stack'] = frames[-24:]
            write((json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8'))
        except Exception:
            fallback()
        return error_id

    # 初始化失败也返回可用回调；后续数据读取不依赖日志目录可写。
    try:
        with _desktop_log_directory(folder):
            pass
    except Exception:
        fallback()
    return safe_event


def desktop_main():
    parser = argparse.ArgumentParser(description='本人账号 ERP 批量取数：TikTok / 集团二部 / 四类候选')
    parser.add_argument('--username', help='本人ERP账号；不传则交互输入')
    parser.add_argument('--salesperson', help='指定销售员姓名，如杨颖；指定时只抓两类亏损')
    parser.add_argument('--source', choices=SOURCES, help='仅抓指定来源；默认抓四类')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent/'output')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--max-orders', type=int, default=20, help='试抓财务订单数，每类默认20；仍保存完整候选名单')
    mode.add_argument('--all', action='store_true', help='获取整个亏损池的订单财务，可能需要数分钟')
    parser.add_argument('--batch-size', type=int, default=1000)
    parser.add_argument('--check', action='store_true', help='仅检查运行环境，不登录、不抓数')
    args = parser.parse_args()
    if args.max_orders < 1 or not 1 <= args.batch_size <= 1000:
        parser.error('试抓数量必须大于0，每批订单数须在1至1000之间')
    if args.check:
        print('环境检查通过：Python、httpx、openpyxl 可用；未发起网络请求。')
        return 0
    username = (args.username or input('请输入你自己的ERP账号：')).strip()
    password = getpass.getpass('ERP密码（输入不显示，不保存）：')
    if not username or not password:
        print('账号和密码不能为空。')
        return 1
    settings = SimpleNamespace(erp_username=username,erp_password=SimpleNamespace(get_secret_value=lambda:password),
        erp_capture_platform='Tiktok',erp_capture_platform_id='9B583641B0CA11EDAAB1AC1F6B9B9987',
        erp_capture_bloc='集团二部',
        erp_capture_request_interval_seconds=16,runtime_dir=args.output.resolve())
    print('范围：TikTok / 集团二部；两类亏损仅财务复核待处理，两类禁止发货按名单取数。',flush=True)
    print('销售员：'+(args.salesperson or '不限'),flush=True)
    print('模式：'+('全量' if args.all else f'试抓前 {args.max_orders} 笔财务；保留每类完整候选名单'),flush=True)
    print('优先查看订单核对表.xlsx：前82列同手动订单导出，附加字段在末尾；原件分别保留。',flush=True)
    print('只下载与保存，不提交调价。密码不会写入脚本或结果。',flush=True)
    log_event = _desktop_logger(args.output)
    log_event('erp.started', stage='capture')
    try:
        result = capture_erp_bulk(settings,source=args.source,max_orders=None if args.all else args.max_orders,
            export_batch_size=args.batch_size,salesperson=args.salesperson,
            progress=lambda text:print(text,flush=True), event_callback=log_event)
    except (OSError, ValueError, SourceUnavailable) as error:
        error_id = log_event('capture.failed', exception=error, stage='save', error_code='storage_failed')
        print('读取或保存失败，请按错误编号查看output/logs：', error_id)
        return 1
    log_event('capture.saved', stage='save', complete=result['capture_complete'])
    log_event('erp.completed', stage='capture', complete=result['capture_complete'],
              limited=bool(result.get('stopped_by_rate_limit', False)))
    print('结果文件：',result['result_file'])
    print(json.dumps(result['summary'],ensure_ascii=False,indent=2))
    for group in result['groups']:
        if group.get('comparison_file'):
            print('订单核对表：',group['comparison_file']['file'])
        if group['error']: print('需核实：',group['error'])
    ok = result['selection_complete'] if result['sample_only'] else result['capture_complete']
    print('本次选择范围完成：',ok,'；整个来源池完成：',result['capture_complete'])
    print('订单缺失或任务失败时返回1；采集完成不等于具备提价资格。')
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        raise SystemExit(desktop_main())
    except KeyboardInterrupt:
        print('已取消；此前完成的批次保留在output，未重新提交任务。')
        raise SystemExit(130)
    except (OSError,EOFError,ValueError):
        print('无法完成输入或保存，请检查终端、输出目录和磁盘空间。')
        raise SystemExit(1)
