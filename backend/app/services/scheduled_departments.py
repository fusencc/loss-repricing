"""四部门日任务。采集只读，正式执行与价格回读使用持久化的业务批次。"""
from app.adapters.base import SourceUnavailable
from app.db import transaction
from app.models import Job
from app.observability import event
from app.services.erp_workflow import run_workflow


def run_daily_departments(service, job, day):
    from app.services.erp_department_pricing import load_department_batch

    config = service.config
    capture = run_workflow(config.model_copy(update={"live_submit_enabled": False}))
    if not capture["departments"]:
        raise SourceUnavailable("未取得当日 ERP 集团采集结果")

    batches = dict(job.payload.get("department_batches", {}))
    unavailable = []
    failures = []
    for scope in capture["departments"]:
        name = scope["department"]
        if scope["status"] == "failed":
            unavailable.append(name)
            continue
        try:
            prepared = load_department_batch(config.runtime_dir, day, scope)
            if prepared is not None:
                batches[name] = service.run_department_batch(job.id, prepared)
            elif scope.get("ready_prices"):
                unavailable.append(name)
        except SourceUnavailable:
            unavailable.append(name)
        except Exception as exc:
            event("erp.failed", exception=exc, job_id=job.id, stage="execute", source="erp")
            failures.append(name)

    with transaction(service.factory) as session:
        current = session.get(Job, job.id)
        current.payload = {**current.payload, "group_capture_id": capture.get("group_capture_id"),
                           "department_batches": batches}
    if failures:
        raise RuntimeError("部门执行失败：" + "、".join(failures))
    if unavailable:
        raise SourceUnavailable("部门来源暂不可用：" + "、".join(unavailable))
    return next(iter(batches.values())) if len(batches) == 1 else None
