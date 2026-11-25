"""把名单采集阶段的官方三天保护结果落为只读业务日志，不创建上传任务。"""
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.db import transaction
from app.domain.types import Decision, VariantKey
from app.models import Audit, Batch, Item
from app.services.erp_capture_store import load_capture
from app.services.erp_pricing import _load_pricing_artifact
from app.services.erp_workflow import ERP_ROSTER_DEPARTMENTS
from app.services.store import stamp


def save_roster_filter_records(factory, runtime_dir, job_id, day, scope):
    """按原任务/人员/日期幂等落档；历史补登与在线任务共用同一来源校验。"""
    runtime = Path(runtime_dir).resolve()
    name, department = scope['salesperson'], scope['department']
    if ERP_ROSTER_DEPARTMENTS.get(name) != department or scope.get('mode') != 'capture':
        raise ValueError('过滤日志缺少已确认的名单人员和部门')
    directory = Path(scope['artifact_dir']).resolve()
    if not directory.is_relative_to(runtime / 'erp-workflows'):
        raise ValueError('过滤日志来源不在本运行目录')
    content = (directory / 'result.json').read_bytes()
    archived = json.loads(content)
    for field in ('capture_id', 'preview_id', 'preview_sha256', 'history_filter', 'capture_complete'):
        if archived.get(field) != scope.get(field):
            raise ValueError('过滤日志与原始采集归档不一致')
    report, digest = _load_pricing_artifact(runtime, scope['capture_id'], scope['preview_id'])
    capture = load_capture(runtime, scope['capture_id'])
    if (digest != scope['preview_sha256'] or report.get('business_date') != day.isoformat()
            or report.get('department') != department or report.get('synthetic') is not False
            or capture.get('source_kind') != 'erp_web_backend' or capture.get('sample_only') is not False
            or capture.get('synthetic', False) is not False or capture.get('department') != department
            or (capture.get('salesperson') or {}).get('name') != name):
        raise ValueError('过滤日志的日期、来源、人员或算价归档未通过')
    # 仅明确 count>0 才产生不调价；未知不伪装成零，完整商品键去重，不按 SKU 文本合并。
    protected = {}
    for row in archived.get('history_filter', []):
        if row['status'] != 'history_protected':
            continue
        count = row.get('count')
        if type(count) is not int or count <= 0:
            raise ValueError('三天保护记录缺少明确的正调价计数')
        key = VariantKey.model_validate(row['key'])
        identity = key.grouping()
        if identity in protected and protected[identity][1] != count:
            raise ValueError('同一商品的三天保护计数冲突')
        protected[identity] = (key, count)
    source = {'capture_id': scope['capture_id'], 'preview_id': scope['preview_id'],
              'preview_sha256': digest, 'scope_sha256': sha256(content).hexdigest(),
              'scope_artifact': str(directory / 'result.json'),
              'capture_complete': archived.get('capture_complete') is True,
              'salesperson': name, 'department': department}
    run_key = f'erp-roster:{job_id}:history:{name}:{day.isoformat()}'

    def existing(session):
        batch = session.scalar(select(Batch).where(Batch.run_key == run_key))
        if batch is not None and batch.snapshot.get('erp_roster_filter_log') != source:
            raise ValueError('同一任务的过滤日志来源已改变')
        return batch.id if batch is not None else None

    with factory() as session:
        if saved := existing(session):
            return saved
    # 过滤日志只保存归属及证据索引；原始完整快照留在带哈希的归档文件，
    # 不能把大体积订单快照放入每行 JOIN 的日志批次，避免数百行读取放大内存。
    versions = {'erp_capture_id': scope['capture_id'], 'erp_roster_salesperson': name,
                'erp_roster_department': department}
    snapshot = {'source_versions': versions, 'erp_roster_filter_log': source}
    recorded = stamp()
    batch_id = str(uuid4())
    try:
        with transaction(factory) as session:
            session.add(Batch(id=batch_id, run_key=run_key, business_date=day.isoformat(), mode='live',
                status='completed', created_at=recorded, snapshot=snapshot,
                summary={'不调价': len(protected)}))
            session.flush()
            for identity, (key, count) in protected.items():
                reason = f'近72小时存在调价日志，不调价（官方记录 {count} 条）'
                decision = Decision(key=key, status='不调价', ready=False,
                    reason_code='recent_price_change', reason=reason, source_row_ids=[],
                    trace={'source_versions': versions,
                           'recent_price_filter': {'count': count, 'window_hours': 72, **source}})
                session.add(Item(id=str(uuid4()), batch_id=batch_id, variant_id=identity,
                    key=key.model_dump(mode='json'), status='不调价', phase='excluded', reason=reason,
                    decision=decision.model_dump(mode='json'), updated_at=recorded))
            session.add(Audit(id=str(uuid4()), action='roster.history_logged', actor='system',
                detail={'batch_id': batch_id, 'salesperson': name, 'items': len(protected),
                        'source': source}, created_at=recorded))
    except IntegrityError:
        with factory() as session:
            if saved := existing(session):
                return saved
        raise
    return batch_id
