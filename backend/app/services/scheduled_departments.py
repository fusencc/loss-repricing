"""四部门日任务。已保存的部门批次从未领取的商品继续，无须重采。"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select

from app.adapters.base import SourceUnavailable
from app.adapters.erp_bulk_capture import ERP_DEPARTMENTS
from app.db import transaction
from app.models import Batch, Job
from app.observability import event
from app.services.erp_workflow import run_workflow


def run_daily_departments(service, job, day):
    from app.services.erp_department_pricing import load_department_batch
    from app.services.execution import PricingService

    config = service.config
    batches = dict(job.payload.get('department_batches', {}))
    empty = set(job.payload.get('empty_departments', ()))
    remaining = [name for name in ERP_DEPARTMENTS if name not in batches and name not in empty]
    with service.factory() as session:
        keys = {f'erp-department:{day.isoformat()}:{name}': name for name in remaining}
        saved = {keys[key]: batch_id for key, batch_id in session.execute(select(Batch.run_key, Batch.id)
                 .where(Batch.mode == 'live', Batch.run_key.in_(keys)))} if keys else {}
    collect = tuple(name for name in remaining if name not in saved)
    unavailable, failures = [], []

    def execute(*, scope=None, saved_id=None):
        runner = PricingService(service.factory, service.adapter, config)
        try:
            if saved_id:
                return runner.resume_department_batch(saved_id, day)
            prepared = load_department_batch(config.runtime_dir, day, scope)
            if prepared is None:
                if scope.get('ready_prices'):
                    raise SourceUnavailable('部门算价结果暂不可用')
                return None
            return runner.run_department_batch(job.id, prepared)
        finally:
            runner.close()

    def checkpoint(**changes):
        with transaction(service.factory) as session:
            current = session.get(Job, job.id, with_for_update=True)
            if current.owner == job.owner:
                current.payload = {**current.payload, **changes}

    with ThreadPoolExecutor(max_workers=len(ERP_DEPARTMENTS)) as uploads:
        pending = {uploads.submit(execute, saved_id=batch_id): name for name, batch_id in saved.items()}

        def ready(scope):
            if scope['status'] != 'failed':
                pending[uploads.submit(execute, scope=dict(scope))] = scope['department']

        if collect:
            capture = run_workflow(config.model_copy(update={'live_submit_enabled': False}),
                                   on_department=ready, departments_to_collect=collect)
            if group_capture_id := capture.get('group_capture_id'):
                checkpoint(group_capture_id=group_capture_id)
            unavailable.extend(name for name in collect if name not in pending.values())
        for future in as_completed(pending):
            name = pending[future]
            try:
                batch_id = future.result()
                if batch_id is None:
                    empty.add(name)
                else:
                    batches[name] = batch_id
                checkpoint(department_batches=batches, empty_departments=sorted(empty))
            except SourceUnavailable:
                unavailable.append(name)
            except Exception as exc:
                event('erp.failed', exception=exc, job_id=job.id, stage='execute', source='erp')
                failures.append(name)

    if failures:
        raise RuntimeError('部门执行失败：' + '、'.join(failures))
    if unavailable:
        raise SourceUnavailable('部门来源暂不可用：' + '、'.join(unavailable))
    return next(iter(batches.values())) if len(batches) == 1 else None
