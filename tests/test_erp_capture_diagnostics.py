import httpx
import pytest

from app.adapters.erp_capture import capture_erp_sample
from app.config import Settings


@pytest.mark.parametrize('total', [None, True, -1, '100', {'sensitive': 'do-not-return'}])
def test_invalid_total_is_unknown_but_does_not_prevent_detail_reads(total):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'code': 200, 'success': True, 'data': {'token': 'private-token'}})
        if '/findDetail/' in request.url.path:
            return httpx.Response(200, json={'code': 0, 'data': {'detaillist': [
                {'sku': 'SKU', 'qty': '1', 'currency': 'PHP'}]}})
        return httpx.Response(200, json={'code': 0, 'data': {
            'records': [{'orderid': 'O-1'}], 'total': total}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_erp_sample(Settings(_env_file=None, erp_username='u', erp_password='p'), client=client)
    assert len(seen) == 6  # 登录+四组列表+去重后的一次订单明细；报告不增加外部请求。
    assert all(g['total'] is None for g in result['groups'])
    assert all(g['coverage'] == 'total_unknown' for g in result['validation']['groups'])
    assert result['validation']['summary']['detail_reads_succeeded'] == 1
    assert 'do-not-return' not in str(result) and 'private-token' not in str(result)


@pytest.mark.parametrize('failure,expected', [('unauthorized', 'authentication_rejected'),
                                              ('forbidden', 'access_denied'),
                                              ('timeout', 'read_failed')])
def test_upstream_denial_is_distinct_from_transport_failure(failure, expected):
    def handler(request):
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'code': 200, 'success': True, 'data': {'token': 'private-token'}})
        if failure == 'timeout':
            raise httpx.ReadTimeout('sensitive-upstream-url', request=request)
        return httpx.Response(401 if failure == 'unauthorized' else 403, json={'msg': 'private-response'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = capture_erp_sample(Settings(_env_file=None, erp_username='u', erp_password='p'), client=client)
    report = result['validation']
    assert report['summary']['list_reads_failed'] == 4
    assert all(g['error_kind'] == expected for g in report['groups'])
    assert not report['permission_scope_verified']
    assert 'private-response' not in str(result) and 'sensitive-upstream-url' not in str(result)
