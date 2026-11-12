"""只读销售查询的单次同请求恢复；每个HTTP响应均先归档，不切换合同或旧数据。"""
from time import sleep
import httpx


def read_response(send,archive,*,retry_events,stage):
    """send只可绑定已确认的只读查询；不得用于导出任务创建、审核或价格提交。"""
    event=None
    for attempt in (1,2):
        try:
            response=send()
        except httpx.RequestError as error:
            if event is not None:event['final_error_kind']=type(error).__name__
            if attempt==2:raise
            event={'stage':stage,'error_kind':type(error).__name__,'http_recovered':False}
            retry_events.append(event);sleep(1);continue
        archive(response,attempt)
        if event is not None:event['http_recovered']=200<=response.status_code<300
        if attempt==1 and 400<=response.status_code<600 and response.status_code not in (401,403):
            event={'stage':stage,'error_kind':'HTTP'+str(response.status_code),'http_recovered':False}
            retry_events.append(event);sleep(1);continue
        return response
