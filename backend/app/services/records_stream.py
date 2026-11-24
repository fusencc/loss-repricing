"""部门日志实时只读投影：仅读取已提交事务，不触发采集、上传或核验。"""
import asyncio
import json

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.services.records import department_records


POLL_SECONDS = 1


def records_payload(factory, department):
    # 每次独立短会话，避免长连接持有事务/旧快照。复用日志投影，状态和金额不另算。
    with factory() as session:
        return encode_records(department_records(session, department))


def encode_records(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


async def stream_response(request, read_payload):
    try:
        initial = await asyncio.to_thread(read_payload)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return StreamingResponse(record_events(request, read_payload, initial),
                             media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


async def record_events(request, read_payload, initial):
    """推送完整已保存视图；重连重新读取，不依赖进程内事件或 worker 同进程。"""
    previous = initial
    yield f'data: {initial}\n\n'
    while not await request.is_disconnected():
        await asyncio.sleep(POLL_SECONDS)
        if await request.is_disconnected():
            return
        # 同步数据库查询移入线程，不占用 API 事件循环；断连取消由流响应处理。
        current = await asyncio.to_thread(read_payload)
        if current != previous:
            previous = current
            yield f'data: {current}\n\n'
        else:
            # 心跳不刷新业务状态，也不把受理/模拟结果改成已生效。
            yield ': keepalive\n\n'
