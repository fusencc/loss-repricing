"""按 ERP 网页合同取得四类候选，并逐批核对财务导出的订单范围。"""
from contextlib import contextmanager
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import sys
from time import monotonic, sleep
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
import warnings
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError
from openpyxl.utils.exceptions import InvalidFileException

import httpx
from openpyxl import load_workbook, Workbook

from app.adapters.base import SourceUnavailable
from app.adapters.erp_web import WEB_BASE, ErpAccountRateLimited, ErpBusinessFailure, error_evidence
from app.adapters.transport_privacy import private_request
from app.services.erp_capture_store import write_verified

# 已观察的ERP网页合同：亏损池下载、禁止发货列表、订单表异步导出及任务查询。
# SOURCES 为本项目来源标识，不是 ERP 字段。
# 2026-11-09杨颖手动导出的82列，仅固定展示顺序，不改变金额或行顺序。
MANUAL_ORDER_HEADERS = ('订单编号', '订单状态', '是否缺货', '订单属性', '交易号', '客户账号', '客户姓名', '买家姓名', '客户邮箱', '电话', '国家(中)', '国家(英)', '所属地区', '所属城市', '邮政编码', '邮寄地址', '货运单号', '货运方式', '运费金额', '订单金额', '重量', '备注信息', '创建时间', '订单付款时间', '订单发货时间', '最迟发货时间', '单号过期时间', '作废时间', '自选物流', '平台', '店铺', '包材', '预估重量', '预估运费', '真实重量', '实际运费', '商品SKU列表', '商品名清单', '商品报关名', '订单成本', '商品成本', '订单自定义状态', '支付方式', '作废原因', '销售员', '部门', '集团', '毛利', '人工成本', '头程运费', '实体仓库名称', '客服专员', 'item ID', '平台交易费', '退款率', '退款费用', '刊登费率', '刊登费用', '库存损益率', '库存损益', 'VAT税率', 'VAT税', '营销费率', '营销费用', '炒分费率', '炒分费用', 'VPS及平台推广费率', 'VPS及平台推广费', '资金费率', '资金费用', '包材费', '仓储费', '退件上架返还费率', '退件上架返还费用', '货运方式对应的报价', '抛重运费', '仓库人力成本单价', '仓库人力成本', '配置费用', '修正毛利', '修正一次毛利', '上线毛利')

SOURCES = {
    'loss_allocatable': ('loss', 4),
    'loss_pending': ('loss', 3),
    'blocked_allocatable': ('blocked', 4),
    'blocked_pending': ('blocked', 3),
}
ERP_DEPARTMENTS = ('Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部')
BLOCKED_LIST = '/Api/order/list'
STATUS_LABELS = {4: '可配货', 3: '待处理'}
LOSS_EXPORT = '/Api/deficit/dowDeficitOrder'
ORDER_EXPORT = '/newIp/report/orderDownload/asyncOrderListDownload'
TASKS = '/newIp/report/download/list'
# 按16秒安全间隔最多等待约1小时；真实历史明细任务已观察到9分33秒。
EXPORT_POLL_LIMIT = 225
# 原币/人民币含义沿用导出列，不在此猜币种或更改金额口径。无源日期字段不补抓取日期。
# 字段核实边界：以下为JSON白名单，原始XLSX保留完整列；一行仍是来源订单表行，
# “商品SKU列表”不是已规范化的子SKU明细，也不提供可直接分摊的单件亏损。
# 费率列不擅自按百分数/小数换算，金额不换汇；缺列不补值、空单元格保留None。
# _value仅将Excel读取值序列化（浮点转字符串不做金额运算），不保证恢复源文件外精度；
# 原始文件是数值核对依据。queried_at是请求时间，非费用生效日或固定业务快照时间。
BUSINESS_FIELDS = ('订单编号','订单状态','是否缺货','订单属性','平台','店铺','部门','集团',
    '销售员','创建时间','订单付款时间','订单发货时间','作废时间','订单自定义状态','商品SKU列表',
    '订单金额','运费金额','订单成本','商品成本','毛利','人工成本','头程运费','实体仓库名称',
    'item ID','平台交易费','退款率','退款费用','刊登费率','刊登费用','库存损益率','库存损益',
    'VAT税率','VAT税','营销费率','营销费用','炒分费率','炒分费用','VPS及平台推广费率',
    'VPS及平台推广费','资金费率','资金费用','包材费','仓储费','退件上架返还费率',
    '退件上架返还费用','仓库人力成本单价','仓库人力成本','配置费用','修正毛利','修正一次毛利','上线毛利')
LOSS_FIELDS = ('销售员','订单编号','订单状态','自定义状态','平台','店铺','集团','部门','订单时间',
               '毛利','人力成本','差额','拉亏损时间','财务复核','自动放行时间')


def _value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (float, Decimal)):
        return str(value)
    return value


def _read_workbook(content, fields):
    """只读来源文件；POI导出dimension可能仅A1，实际数据需顺序扫描XML。

    订单号必须原始文本，数值订单号不可修复其可能丢失的精度。空值保留None。
    """
    if not content.startswith(b'PK'):
        raise SourceUnavailable('ERP下载内容不是XLSX文件')
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Workbook contains no default style.*', category=UserWarning)
        book = load_workbook(BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = book.active
        sheet.reset_dimensions()
        iterator = sheet.iter_rows(values_only=True)
        headers = next(iterator, ())
        required = {'订单编号', '订单状态', '平台', '集团'}
        if not required <= set(headers):
            raise SourceUnavailable('ERP导出缺少订单标识或范围字段')
        if fields == BUSINESS_FIELDS and not {'毛利','修正毛利','修正一次毛利'} <= set(headers):
            raise SourceUnavailable('ERP订单导出缺少财务字段')
        business_columns = [(name, i) for i, name in enumerate(headers) if name in fields]
        positions = dict(business_columns)
        if len(positions) != len(business_columns):
            raise SourceUnavailable('ERP导出包含重复业务列，无法确认列值')
        records = []
        for values in iterator:
            if all(v is None for v in values):
                continue
            row = {name: _value(values[i]) if i < len(values) else None for name, i in positions.items()}
            if not isinstance(row.get('订单编号'), str) or not row['订单编号'].strip():
                raise SourceUnavailable('ERP导出订单编号不是完整文本')
            records.append(row)
        return records, len(headers)
    finally:
        book.close()


def _workbook(content, fields):
    try:
        return _read_workbook(content, fields)
    except (BadZipFile, ParseError, InvalidFileException, KeyError, EOFError) as error:
        raise SourceUnavailable('ERP下载文件损坏或XLSX结构无效') from None


def _scope(records, bloc, *, expected_status=None, department=None, salesperson=None):
    if any(not isinstance(r.get('平台'), str) or r['平台'].casefold() != 'tiktok'
           or (expected_status is not None and r.get('订单状态') != expected_status) or r.get('集团') != bloc for r in records):
        raise SourceUnavailable('ERP导出范围与TikTok/订单状态/集团筛选不一致')
    # 禁止发货列表的部门列为空，按明确订单号下载后用财务表「部门」逐行复核。
    if department is not None and any(
        not isinstance(row.get('部门'), str) or row['部门'].casefold() != department.casefold()
        for row in records
    ):
        raise SourceUnavailable('ERP导出包含部门缺失或与指定部门不一致的订单')
    if salesperson is not None and any(row.get('销售员') != salesperson for row in records):
        raise SourceUnavailable('ERP导出销售员字段缺失或与指定销售员不一致')


@contextmanager
def _session(settings, client=None):
    if not settings.erp_username or not settings.erp_password.get_secret_value():
        raise SourceUnavailable('请配置ERP账号密码')
    owned = client is None
    client = client or httpx.Client(timeout=120, trust_env=False, follow_redirects=False)
    context = private_request.set(True)
    last = None
    interval = settings.erp_capture_request_interval_seconds if owned else 0
    try:
        def request(method, path, **kwargs):
            nonlocal last
            if last is not None:
                sleep(max(0, interval - (monotonic() - last)))
            last = monotonic()
            r = client.request(method, path, timeout=120, follow_redirects=False, **kwargs)
            r.raise_for_status()
            if 300 <= r.status_code < 400:
                raise SourceUnavailable('ERP下载重定向未验证，未继续下载')
            return r

        def payload(response, expected=200):
            j = response.json()
            if not isinstance(j, dict) or type(j.get('code')) is not int:
                raise SourceUnavailable('ERP批量接口返回结构无效')
            if j['code'] == 500003:
                raise ErpAccountRateLimited()
            if j['code'] != expected:
                raise ErpBusinessFailure(j['code'])
            return j.get('data')

        user = payload(request('POST', WEB_BASE + '/newIp/system/login', json={
            'username': settings.erp_username, 'password': settings.erp_password.get_secret_value()}))
        token = user.get('accessToken') or user.get('token') if isinstance(user, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise SourceUnavailable('ERP登录未返回令牌')

        def post(path, body, binary=False, *, success_code=None):
            expected = success_code if success_code is not None else (0 if path == BLOCKED_LIST else 200)
            if type(expected) is not int or expected not in (0, 200):
                raise ValueError('ERP成功码须为已核对的0或200')
            r = request('POST', WEB_BASE + path, json=body, headers={'authorization': token})
            if binary and r.content.startswith(b'PK'):
                return r.content
            data = payload(r, expected=expected)
            if binary:
                raise SourceUnavailable('ERP亏损下载未返回XLSX')
            return data

        def download(address):
            if not isinstance(address, str):
                raise SourceUnavailable('ERP任务缺少下载地址')
            target = urljoin(WEB_BASE, address)
            parsed = urlsplit(target)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                raise SourceUnavailable('ERP下载地址格式无效')
            # 令牌仅发送到ERP源，带权限下载URL只在内存中使用，不写日志或采集文件。
            headers = {'authorization': token} if parsed.netloc == urlsplit(WEB_BASE).netloc else {}
            return request('GET', target, headers=headers).content

        yield post, download
    finally:
        if owned:
            client.close()
        private_request.reset(context)


def _save_xlsx(folder, name, content):
    path = folder / name
    path.write_bytes(content)
    restored = path.read_bytes()
    if restored != content:
        raise OSError('ERP原始下载文件回读不一致')
    return {'file': str(path), 'bytes': len(content), 'sha256': sha256(restored).hexdigest()}


def _all_export_rows(path):
    """读取原件全部列，不使用业务JSON白名单；空值不补零。"""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book.active
        sheet.reset_dimensions()
        iterator = sheet.iter_rows(values_only=True)
        headers = tuple(next(iterator, ()))
        return headers, [dict(zip(headers, row)) for row in iterator
                         if any(value is not None for value in row)]
    finally:
        book.close()


def write_comparison_workbook(folder, financial_paths, loss_path, *, filename='订单核对表.xlsx'):
    """独立核对表：前82列依手动订单表排列，新增财务列及亏损池独有列追加。

    原始XLSX不重写；按订单号补充来源列，不重算、不合并/去重财务行、不按池序重排行。
    财务表缺列明确记录，核对表对应格留空；并非来源字段已完整或公式已复算。
    """
    pool_headers, pool_rows = _all_export_rows(loss_path)
    pool = {row['订单编号']: row for row in pool_rows}
    extra_finance = []
    data = []
    missing = set()
    for path in financial_paths:
        headers, rows = _all_export_rows(path)
        missing.update(set(MANUAL_ORDER_HEADERS) - set(headers))
        extra_finance.extend(h for h in headers if h not in MANUAL_ORDER_HEADERS
                             and h not in extra_finance)
        data.extend(rows)
    pool_extra = [h for h in pool_headers if h not in MANUAL_ORDER_HEADERS]
    headers = list(MANUAL_ORDER_HEADERS) + extra_finance + ['亏损池·' + h for h in pool_extra]
    book = Workbook()
    sheet = book.active
    sheet.title = '订单核对'
    sheet.append(headers)
    for row_number, row in enumerate(data, start=2):
        values = [row.get(h) for h in list(MANUAL_ORDER_HEADERS) + extra_finance]
        values += [pool.get(row.get('订单编号'), {}).get(h) for h in pool_extra]
        sheet.append(values)
        # 来源文本始终按文本写入，避免以等号开头的商品/备注被解释为公式。
        for column, value in enumerate(values, start=1):
            if isinstance(value, str):
                sheet.cell(row_number, column).data_type = 's'
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    buffer = BytesIO()
    book.save(buffer)
    book.close()
    result = _save_xlsx(Path(folder), filename, buffer.getvalue())
    result.update(kind='derived_comparison', rows=len(data),
                  reference_columns=82, missing_reference_columns=sorted(missing))
    return result


def _blocked_ids(post, group, *, salesperson=None):
    """列表确认 jzfh=1；异步按条件导出实测会混入非禁止发货订单。"""
    if salesperson is not None:
        seller_id = group['filters'].get('shopUsernameid')
        if not isinstance(seller_id, str) or not seller_id or seller_id != seller_id.strip():
            raise SourceUnavailable('ERP禁止发货名单缺少有效的销售员ID')
    ids = {}
    # 2026-11-11两类真实读取已验证limit=10000，分别一次返回2467/4431条且等于total。
    # 单页取得常见业务范围，避免跨16秒请求间隔时订单状态变化导致名单漂移；
    # 超过一页仍按原完整性条件分页，不放宽总数、页长或逐行范围检查。
    page_size = 10000
    page = 1
    while True:
        data = post(BLOCKED_LIST, dict(group['filters'], page=page, limit=page_size))
        records = data.get('records') if isinstance(data, dict) else None
        total = data.get('total') if isinstance(data, dict) else None
        if type(total) is not int or total < 0 or not isinstance(records, list):
            raise SourceUnavailable('ERP禁止发货分页结构无效')
        group['pages'].append({'page':page, 'total':total, 'rows':len(records)})
        if page == 1:
            group['total'] = total
        if total != group['total'] or len(records) != min(page_size, max(0,total-(page-1)*page_size)):
            raise SourceUnavailable('ERP禁止发货分页总数或页长变化，未使用不完整名单')
        for row in records:
            order = row.get('orderid') if isinstance(row, dict) else None
            if (not isinstance(order, str) or not order.strip() or order in ids
                    or str(row.get('platform')).casefold() != 'tiktok'
                    or str(row.get('jzfh')) != '1' or str(row.get('status')) != str(group['status'])):
                raise SourceUnavailable('ERP禁止发货名单存在重复订单、无效订单号或范围不符')
            # ERP 订单页按 shopUsernameid 查询，列表「销售员」返回姓名 shopUsername。
            # 每页先验归属再使用名单，包括试抓额度之外的订单。
            if salesperson is not None and row.get('shopUsername') != salesperson:
                raise SourceUnavailable('ERP禁止发货名单销售员字段缺失或与指定销售员不一致')
            ids[order] = {key: row[key] for key in ('shopid', 'shopname')
                          if isinstance(row.get(key), str) and row[key].strip()}
        if len(ids) == total:
            if 'departments' in group['filters']:
                group['blocked_shops'] = ids
            return list(ids)
        page += 1


def finance_batch_scope(order_ids, downloaded_ids):
    """按文本订单号核对一个财务子批，保留缺单、越界和重复行证据。"""
    expected = set(order_ids)
    actual = set(downloaded_ids)
    return {
        'requested': len(expected),
        'downloaded_rows': len(downloaded_ids),
        'downloaded_unique': len(actual),
        'missing': sorted(expected - actual),
        'unexpected': sorted(actual - expected),
        'duplicate_rows': len(downloaded_ids) - len(actual),
    }


def _finance_batches(group, ids, batch_size):
    batches = [
        {'number': number, 'order_ids': ids[start:start + batch_size], 'status': 'not_attempted', 'polls': []}
        for number, start in enumerate(range(0, len(ids), batch_size), start=len(group['batches']) + 1)
    ]
    group['batches'].extend(batches)
    return batches


def capture_erp_bulk(settings, *, source=None, client=None, max_polls=EXPORT_POLL_LIMIT, export_batch_size=1000,
                     max_orders=None, progress=None, salesperson=None, department=None, event_callback=None):
    """默认核对四类候选。显式试抓按每组限额，保留完整候选名单。"""
    if source is not None and source not in SOURCES:
        raise ValueError('未知的ERP候选来源')
    if department is not None and department not in ERP_DEPARTMENTS:
        raise ValueError('未知的ERP采集部门')
    sources = [source] if source else list(SOURCES)
    if settings.erp_capture_platform.casefold() != 'tiktok':
        raise ValueError('当前批量下载仅支持TikTok')
    if type(max_polls) is not int or max_polls < 1:
        raise ValueError('任务查询次数必须为正整数')
    if type(export_batch_size) is not int or not 1 <= export_batch_size <= 1000:
        raise ValueError('每批订单数必须为1至1000')
    if max_orders is not None and (type(max_orders) is not int or max_orders < 1):
        raise ValueError('小量试抓订单数必须为正整数')
    if salesperson is not None and (not isinstance(salesperson, str) or not salesperson.strip()):
        raise ValueError('销售员姓名不能为空')
    salesperson = salesperson.strip() if salesperson is not None else None
    emit = progress or (lambda message: None)
    # 与桌面独立构建共享核心：日志只经注入回调传递静态阶段和计数，
    # 不把销售员、订单、任务名、下载地址或进度文案作为日志字段。
    warned = False

    def log_event(name, **fields):
        # 日志是旁路观测；注入回调的磁盘/编码等故障不能中断读取、改变限流处理或重提导出。
        nonlocal warned
        if event_callback is None:
            return None
        try:
            return event_callback(name, **fields)
        except Exception:
            if not warned:
                warned = True
                try:
                    sys.stderr.write('ERP技术日志暂不可用；读取及证据保存流程保持原规则。\n')
                except Exception:
                    pass
            return None

    started = monotonic()
    log_event('erp.stage', stage='auth')
    now = datetime.now(timezone.utc)
    folder = Path(settings.runtime_dir) / 'source-captures' / ('erp-bulk-' + now.strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex)
    folder.mkdir(parents=True)
    groups = []
    for name in sources:
        kind, status = SOURCES[name]
        if kind == 'loss':
            # ERP 待复核在导出表中可显示为空白；源端 financecheck=3 排除自动放行。
            filters = {'platform':settings.erp_capture_platform, 'bloc':settings.erp_capture_bloc,
                       'status':status, 'financecheck':'3'}
            if salesperson is not None:
                filters['username'] = salesperson
            if department is not None:
                filters['department'] = department
        else:
            filters = {'ptid':settings.erp_capture_platform_id, 'blocs':settings.erp_capture_bloc,
                       'status':status, 'jzfh':'1'}
            if department is not None:
                filters['departments'] = department
        groups.append({'source':name,'kind':kind,'status':status,'filters':filters,
                       'endpoint':LOSS_EXPORT if kind == 'loss' else BLOCKED_LIST,
                       'rows':[],'loss_rows':[],'candidate_order_ids':[],'selected_orders':0,
                       'total':None,'error':None,'failed_pages':[],'files':[],'pages':[],
                       'list_complete':False,'stage':'not_attempted','batches':[],
                       'status_discrepancies':[],'missing_order_ids':[]})
    result = {'source_version':'erp-web-bulk-four-20261109','source_kind':'erp_web_backend',
              'sample_only':max_orders is not None,'selection_complete':False,
              'acquisition_mode':'bulk_download','queried_at':now.isoformat(),'groups':groups,
              'details':[],'capture_complete':False,'pricing_ready':False,'stopped_by_rate_limit':False,
              'finance_calculation':{'status':'not_recomputed','reason':'本轮保存ERP导出费用及毛利；文件不包含全部前端公式输入，不逐单补查或补零'}}
    if department is not None:
        result['department'] = department

    def collect_candidates(post, group):
        name = group['source']
        group['stage'] = 'candidate_list'
        log_event('erp.stage', stage='pool')
        if group['kind'] == 'loss':
            raw = post(LOSS_EXPORT, group['filters'], binary=True)
            group['files'].append(_save_xlsx(folder, f'{name}-loss-orders.xlsx', raw))
            losses, count = _workbook(raw, LOSS_FIELDS)
            _scope(losses, settings.erp_capture_bloc, expected_status=STATUS_LABELS[group['status']],
                   department=department, salesperson=salesperson)
            group.update(loss_rows=losses, total=len(losses), loss_column_count=count)
            ids = list(dict.fromkeys(r['订单编号'] for r in losses))
        else:
            ids = _blocked_ids(post, group, salesperson=salesperson)
        group.update(candidate_order_ids=ids, list_complete=True)

    def download_finance(post, download, group, batch):
        name = group['source']
        selected_count = group['selected_orders']
        emit(f"提交 {name} 第 {batch['number']}/{len(group['batches'])} 批，共 {len(batch['order_ids'])} 笔…")
        group['stage'] = 'submit_order_export'
        log_event('erp.stage', stage='export', count=len(batch['order_ids']), total=selected_count)
        batch['status'] = 'submitting'
        response = post(ORDER_EXPORT, {'orderIdList':batch['order_ids']})
        match = re.search(r'下载任务【([^】]+)】', response) if isinstance(response,str) else None
        if not match:
            raise SourceUnavailable('ERP未返回可绑定的下载任务名，未按最新任务猜测')
        batch['task_name'] = match[1]
        batch['status'] = 'polling'
        group['stage'] = 'poll_order_export'
        for attempt in range(max_polls):
            emit(f'等待本批下载任务：第 {attempt+1}/{max_polls} 次查询…')
            # 只查询任务列表第一页100条，按本批返回名精确匹配并校验后续ID。
            # 未出现在这一页就继续有限轮询，不翻页、不选最新任务，也不重提导出。
            log_event('erp.stage', stage='poll', count=attempt + 1, total=max_polls)
            data = post(TASKS, {'pageNum':1,'pageSize':100,'source':'orderList'})
            tasks = data.get('records') if isinstance(data, dict) else data
            if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
                raise SourceUnavailable('ERP下载任务列表格式无效')
            tasks = [t for t in tasks if t.get('source') == 'orderList' and t.get('fileName') == batch['task_name']]
            if len(tasks) > 1:
                raise SourceUnavailable('ERP下载任务名存在多条匹配')
            task = tasks[0] if tasks else None
            batch['polls'].append({'attempt':attempt+1,'queried_at':datetime.now(timezone.utc).isoformat(),
                                  'status':task.get('status') if task else 'not_found'})
            if task:
                if task.get('id') is None or ('task_id' in batch and task['id'] != batch['task_id']):
                    raise SourceUnavailable('ERP下载任务编号发生变化')
                batch['task_id'] = task.get('id')
                group['task_id'] = task.get('id')
                if task.get('status') in ('failure','failed','interrupted'):
                    raise SourceUnavailable('ERP后台导出任务失败')
                if task.get('status') == 'success':
                    group['stage'] = 'download_order_export'
                    raw = download(task.get('downloadUrl'))
                    # 这是原件字节接收完成的观察时间，区别于 ERP 任务状态时间。
                    batch['downloaded_at'] = datetime.now(timezone.utc).isoformat()
                    group['files'].append(_save_xlsx(folder, f"{name}-order-finance-{batch['number']:03d}.xlsx", raw))
                    orders, count = _workbook(raw, BUSINESS_FIELDS)
                    _scope(orders, settings.erp_capture_bloc, department=department, salesperson=salesperson)
                    scope = finance_batch_scope(batch['order_ids'], [row['订单编号'] for row in orders])
                    batch['scope_check'] = scope
                    missing = scope['missing']
                    duplicates = scope['duplicate_rows']
                    if scope['unexpected']:
                        raise SourceUnavailable('ERP批量订单导出包含未请求的订单号')
                    group['missing_order_ids'].extend(missing)
                    # 亏损池状态可能滞后于订单主表（本轮已见已完成/作废）；
                    # 保留两个时点的证据而非丢数据，也不把历史池记录当可直接调价。
                    mismatches = [{'orderid':r['订单编号'],'candidate_status':STATUS_LABELS[group['status']],
                                   'order_status':r.get('订单状态')} for r in orders
                                  if r.get('订单状态') != STATUS_LABELS[group['status']]]
                    group['status_discrepancies'].extend(mismatches)
                    batch['status_discrepancy_count'] = len(mismatches)
                    group['rows'].extend({'orderid':r['订单编号'],'platform':r['平台'],
                        'source_status':group['status'],'current_status_matches_source':r.get('订单状态') == STATUS_LABELS[group['status']],
                        'fields':r} for r in orders)
                    group['order_column_count'] = count
                    batch['status'] = 'partial' if missing or duplicates else 'complete'
                    if duplicates:
                        batch.update(error='ERP订单财务导出包含同订单重复行',
                                     error_kind='duplicate_finance_rows')
                    elif missing:
                        batch['error'] = '部分候选订单未由完整订单批量导出返回'
                        batch['error_kind'] = 'not_returned_by_bulk_export'
                    emit(f"本批返回 {scope['downloaded_unique']} 笔，缺失 {len(missing)} 笔。")
                    break
        if batch['status'] not in ('complete','partial'):
            raise SourceUnavailable('ERP后台下载任务在查询次数内未完成；未重新提交')

    def finish_group(group):
        if group['error'] or not group['list_complete']:
            return
        if group['selected_orders'] != len(group['candidate_order_ids'][:max_orders]) or any(
            batch['status'] not in ('complete', 'partial') for batch in group['batches']
        ):
            return
        name = group['source']
        missing = group['missing_order_ids']
        # 财务表按订单导出。重复行保留供回查，但不能证明订单财务已唯一对齐。
        duplicate_rows = sum(batch['scope_check']['duplicate_rows'] for batch in group['batches'])
        if duplicate_rows:
            group.update(stage='finance_conflict', error=f'ERP订单财务导出存在{duplicate_rows}条重复行',
                         error_kind='duplicate_finance_rows')
        elif missing:
            group.update(stage='complete_with_missing',
                         error=f'完整订单批量导出未返回{len(missing)}笔候选订单，已保留缺失清单',
                         error_kind='not_returned_by_bulk_export')
        else:
            group['stage'] = 'complete'
        finance_paths = [Path(item['file']) for item in group['files']
                         if '-order-finance-' in Path(item['file']).name]
        if finance_paths and group['kind'] == 'loss':
            group['comparison_file'] = write_comparison_workbook(
                folder, finance_paths, folder / f'{name}-loss-orders.xlsx',
                filename=f'{name}-订单核对表.xlsx')

    @contextmanager
    def record_group_errors(group, batch=None):
        try:
            yield
        except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError, OSError) as error:
            stage = {'submit_order_export':'export', 'poll_order_export':'poll',
                     'download_order_export':'download'}.get(group['stage'], 'pool')
            log_event('erp.failed', exception=error, stage=stage,
                      error_code='rate_limited' if isinstance(error, ErpAccountRateLimited) else 'source_unavailable')
            evidence = error_evidence(error)
            group.update(evidence)
            if batch is not None:
                batch.update(status='failed', **evidence)
            if isinstance(error, ErpAccountRateLimited):
                raise
        finally:
            # 每批落盘，后续批次失败也可查看已成功部分，不重复生成任务。
            write_verified(folder / 'result.json', result)

    emit('正在登录ERP，按来源读取候选名单…')
    try:
        with _session(settings, client) as (post, download):
            if salesperson is not None:
                users = post('/Api/daysell/selectSellman', {})
                if not isinstance(users, list):
                    raise SourceUnavailable('ERP销售员选项返回结构无效')
                matches = [u for u in users if isinstance(u, dict) and u.get('userName') == salesperson]
                if len(matches) != 1:
                    raise SourceUnavailable('销售员不存在或同名选项不唯一，请核实ERP下拉选项')
                seller_id = matches[0].get('userID')
                result['salesperson'] = {'name': salesperson, 'id': seller_id}
                for group in groups:
                    if group['kind'] == 'blocked':
                        group['filters']['shopUsernameid'] = seller_id
                emit(f'已确认销售员：{salesperson}，按所选来源读取该销售员的订单。')
            if department is None:
                for group in groups:
                    with record_group_errors(group):
                        collect_candidates(post, group)
                        ids = group['candidate_order_ids'][:max_orders]
                        group['selected_orders'] = len(ids)
                        batches = _finance_batches(group, ids, export_batch_size)
                        for batch in batches:
                            # Record the current task failure before moving to the next source.
                            with record_group_errors(group, batch):
                                download_finance(post, download, group, batch)
                            if batch['status'] == 'failed':
                                break
                        finish_group(group)
            else:
                for group in groups:
                    with record_group_errors(group):
                        collect_candidates(post, group)
                for group in groups:
                    if not group['list_complete']:
                        continue
                    ids = group['candidate_order_ids'][:max_orders]
                    group['selected_orders'] = len(ids)
                    for batch in _finance_batches(group, ids, export_batch_size):
                        with record_group_errors(group, batch):
                            download_finance(post, download, group, batch)
    except (SourceUnavailable, httpx.HTTPError, ValueError, TypeError, KeyError, OSError) as error:
        result.update(error_evidence(error))
        result['stopped_by_rate_limit'] = isinstance(error, ErpAccountRateLimited)
        if not any(group['error'] for group in groups):
            groups[0].update(error_evidence(error))
    if department is not None:
        for group in groups:
            with record_group_errors(group):
                finish_group(group)
    result['not_read_groups'] = [group['source'] for group in groups if group['stage'] == 'not_attempted']
    result['selection_complete'] = all(group['stage'] == 'complete' and not group['error'] for group in groups)
    result['capture_complete'] = result['selection_complete'] and max_orders is None
    rows = [row for group in groups for row in group['rows']]
    for group in groups:
        group['fetched_rows'] = len(group['rows'])
    result['summary'] = {
        'selected_orders':sum(group['selected_orders'] for group in groups),
        'orders_not_selected':sum(len(group['candidate_order_ids'])-group['selected_orders'] for group in groups),
        'list_rows':len(rows), 'unique_orders':len({row['orderid'] for row in rows}),
        'loss_rows':sum(len(group['loss_rows']) for group in groups), 'detail_rows':0,
        'status_discrepancies':sum(len(group['status_discrepancies']) for group in groups),
        'missing_orders':sum(len(group['missing_order_ids']) for group in groups),
        'order_status_counts':dict(Counter(str(row['fields']['订单状态']) if row['fields']['订单状态'] is not None
                                          else '未知/空值' for row in rows)),
    }
    write_verified(folder / 'result.json', result)
    result['result_file'] = str(folder / 'result.json')
    log_event('erp.stage', stage='save', count=len(rows), complete=result['capture_complete'],
              limited=result['stopped_by_rate_limit'], duration_ms=round((monotonic()-started)*1000))
    emit('采集结果和原始Excel已保存并回读。')
    return result
