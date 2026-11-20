"""ERP抽样证据汇总；只描述已观察事实，不推导业务公式或账号授权范围。

依据 docs/erp-source-capture.md。total 是当前账号/筛选条件下的接口自报数，
不是全公司总数；第一页全部返回也不能证明权限完整或生成可执行 Snapshot。
报告复用本次采集结果，不增加请求，不参与生产调价的放行判定。
"""
from decimal import Decimal, InvalidOperation

GROUP_NAMES = {
    ('loss', 4): ('loss_allocatable', '亏损订单／可配货'),
    ('loss', 3): ('loss_pending', '亏损订单／待处理'),
    ('blocked', 4): ('blocked_allocatable', '禁止发货／可配货'),
    ('blocked', 3): ('blocked_pending', '禁止发货／待处理'),
}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return False
    try:
        number = Decimal(value)
        return number.is_finite() and number > 0
    except InvalidOperation:
        return False


def build_sample_validation(sample):
    """仅供管理员只读诊断。空列表是观察事实，不能解释为无权限或全量为零。"""
    groups = []
    for group in sample['groups']:
        source, label = GROUP_NAMES[(group['kind'], group['status'])]
        rows, total = group['rows'], group['total']
        count_valid = type(total) is int and total >= 0
        if group['error']:
            coverage = 'read_failed'
        elif not count_valid:
            coverage = 'total_unknown'
        elif total < len(rows) or (total > 0 and not rows):
            coverage = 'count_inconsistent'
        elif total > len(rows):
            coverage = 'first_page_only'
        elif total == 0:
            coverage = 'reported_empty'
        else:
            coverage = 'reported_total_observed'
        shops = sorted({row['shopid'] for row in rows if _text(row.get('shopid'))})
        groups.append({
            'source': source, 'label': label, 'endpoint': group['endpoint'],
            'requested_scope': group['filters'], 'read_succeeded': not bool(group['error']),
            'sampled_rows': len(rows), 'reported_total': total if count_valid else None,
            'coverage': coverage, 'observed_shop_ids': shops,
            'observed_order_ids': sorted({row['orderid'] for row in rows if _text(row.get('orderid'))}),
            'missing_order_ids': sum(not _text(row.get('orderid')) for row in rows),
            'error': group['error'], 'error_kind': group.get('error_kind'),
            'http_status': group.get('http_status'), 'business_code': group.get('business_code'),
            'permission_scope_verified': False,
        })

    detail_issues = []
    detail_rows = 0
    for detail in sample['details']:
        if detail['error']:
            detail_issues.append({'order_id': detail['orderid'], 'row_number': None,
                                  'fields': [], 'reason': detail['error'],
                                  'error_kind': detail.get('error_kind'),
                                  'http_status': detail.get('http_status'),
                                  'business_code': detail.get('business_code')})
            continue
        rows = detail['rows']
        detail_rows += len(rows)
        if not rows:
            detail_issues.append({'order_id': detail['orderid'], 'row_number': None,
                                  'fields': [], 'reason': '明细接口返回空列表，尚无可核对明细'})
        for index, row in enumerate(rows, 1):
            # 仅报告原始字段是否具备，不把currency解释成订单毛利币种，
            # 不把sku/platItemid直接认定为已确认的在线子SKU唯一映射。
            fields = []
            if not _text(row.get('sku')):
                fields.append('sku')
            currency = row.get('currency')
            if not (isinstance(currency, str) and len(currency) == 3
                    and currency.isascii() and currency.isalpha() and currency.isupper()):
                fields.append('currency')
            if not _positive_number(row.get('qty')):
                fields.append('qty')
            if fields:
                detail_issues.append({'order_id': detail['orderid'], 'row_number': index,
                                      'fields': fields, 'reason': '原始明细字段缺失或格式无效，需核实来源'})

    succeeded = sum(group['read_succeeded'] for group in groups)
    return {
        'report_version': 'erp-sample-validation-v1',
        'source_version': sample['source_version'], 'queried_at': sample['queried_at'],
        'authentication': 'authenticated',
        'message': '仅验证本次账号和筛选范围的第一页读取；权限完整性、公式及可执行快照尚未验收。',
        'sample_only': True, 'complete': False, 'pricing_ready': False,
        'permission_scope_verified': False,
        'summary': {
            'list_reads_succeeded': succeeded, 'list_reads_failed': len(groups) - succeeded,
            'sampled_rows': sum(group['sampled_rows'] for group in groups),
            'detail_reads_succeeded': sum(not bool(d['error']) and d['orderid'] is not None for d in sample['details']),
            'detail_reads_failed': sum(bool(d['error']) and d['orderid'] is not None for d in sample['details']),
            'detail_rows': detail_rows, 'detail_issue_count': len(detail_issues),
        },
        'groups': groups, 'detail_issues': detail_issues,
        'pending_confirmations': [*sample['pending_confirmations'],
            '公司完整在线利润分项公式、费用版本及价格适用区间',
            '当日汇率方向与生效日期；查询成功不代表可用于算价',
            '当前零售价/营销价、活动与历史来源，以及上传后折扣保持行为'],
        'next_steps': [
            '使用同一账号、同一平台/部门/状态筛选的人工导出，核对四组总数与订单标识；识别静默权限过滤。',
            '按已授权店铺清单核对可见范围；未出现在样本中的店铺不能直接判为无权限。',
            '确认亏损字段、币种、数量粒度及多SKU归属，再核对完整候选与在线映射。',
            '取得公司批准费用公式和独立样本，核对当前价及目标价毛利；本报告不确认公式等价。',
        ],
    }
