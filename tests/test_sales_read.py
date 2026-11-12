import httpx
import pytest


def test_current_read_request_recovers_once_and_archives_both_responses(monkeypatch):
    from app.adapters.sales_read import read_response
    monkeypatch.setattr('app.adapters.sales_read.sleep',lambda seconds:None)
    attempts=[];archived=[];events=[]
    def send():
        attempts.append(1)
        return httpx.Response(500 if len(attempts)==1 else 200,request=httpx.Request('POST','http://example.test/read'),json={'code':0})
    result=read_response(send,lambda response,attempt:archived.append((response.status_code,attempt)),
        retry_events=events,stage='products')
    assert result.status_code==200 and archived==[(500,1),(200,2)]
    assert events[0]['http_recovered'] is True


@pytest.mark.parametrize('status',[401,403])
def test_authentication_denial_is_not_retried(status):
    from app.adapters.sales_read import read_response
    calls=[];events=[]
    def send():calls.append(1);return httpx.Response(status)
    assert read_response(send,lambda response,attempt:None,retry_events=events,stage='history').status_code==status
    assert len(calls)==1 and not events


def test_read_timeout_repeats_the_same_contract_once(monkeypatch):
    from app.adapters.sales_read import read_response
    monkeypatch.setattr('app.adapters.sales_read.sleep',lambda seconds:None)
    calls=[];events=[]
    def send():
        calls.append(1)
        if len(calls)==1:raise httpx.ReadTimeout('TEST')
        return httpx.Response(200)
    assert read_response(send,lambda response,attempt:None,retry_events=events,stage='history').status_code==200
    assert len(calls)==2 and events[0]['error_kind']=='ReadTimeout'


def test_success_http_is_not_retried_to_hide_business_or_scope_errors():
    from app.adapters.sales_read import read_response
    calls=[]
    def send():calls.append(1);return httpx.Response(200,json={'resp_code':1})
    response=read_response(send,lambda response,attempt:None,retry_events=[],stage='variants')
    assert response.json()['resp_code']==1 and len(calls)==1
