from collections import Counter
from datetime import datetime
import time
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from app.models import Batch, Item, Execution, Job, Protection, Audit, WorkerLease
from app.db import transaction
from app.services.store import settings_value, update_settings, enqueue, utcnow, stamp, BEIJING, current_items
from app.services.store import ERP_RECONCILIATION_PREFIX, is_erp_reconciliation
from app.api.auth import COOKIE, require_admin, require_user, make_session, password_matches
from app.services.coordination import can_access, ensure_demo_ownership, verify_password, principal
from app.models.coordination import UserAccount
from app.api.coordination import can_view_item, item_collaboration
from dataclasses import asdict

router = APIRouter(prefix="/api")
secured = APIRouter(dependencies=[Depends(require_user)])


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=256)


class Changes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool | None = None
    paused_sites: list[Literal["MY", "PH", "SG", "TH", "VN"]] | None = None
    pilot_shops: list[str] | None = Field(default=None, max_length=1000)
    autonomous_enabled: bool | None = None
    site_enabled: list[Literal["MY", "PH", "SG", "TH", "VN"]] | None = None
    schedule_enabled: bool | None = None


class Protect(BaseModel):
    shop_id: str = Field(min_length=1, max_length=100)
    site: Literal["MY", "PH", "SG", "TH", "VN"]
    main_sku: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class Reevaluate(BaseModel):
    note: str = Field(default="", max_length=500)


def columns(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def batch_view(batch):
    value = {k: v for k, v in columns(batch).items() if k not in {"snapshot", "run_key"}}
    if is_erp_reconciliation(batch):
        value["erp_reconciliation"] = batch.snapshot["erp_reconciliation"]
    return value


def item_view(session, item, actor=None):
    value = columns(item)
    from app.domain.types import VariantKey, VariantReference
    value["target_id"] = VariantKey.model_validate(item.key).target_identity()
    decision = value["decision"] or {}
    trace = decision.get("trace") or {}
    if actor and actor.role != "admin" and (issues := trace.get("source_issues")):
        scoped_issues = []
        for issue in issues:
            issue_key = issue.get("key")
            if issue_key and VariantReference.model_validate(issue_key).target_identity() == value["target_id"]:
                scoped_issues.append(issue)
            else:
                # 同主 SKU 或同明细的关联记录可能归其他销售，只展示来源和序号。
                scoped_issues.append({field: issue[field] for field in ("source", "record_number", "relation") if field in issue})
        value["decision"] = {**decision, "trace": {**trace, "source_issues": scoped_issues}}
    execution = session.scalar(select(Execution).where(Execution.item_id == item.id))
    value["execution"] = columns(execution) if execution else None
    value["evidence"] = execution.evidence if execution else {}
    if is_erp_reconciliation(session.get(Batch, item.batch_id)):
        value["read_only"] = True
    elif actor:
        value.update(item_collaboration(session,item,actor))
    return value


def consistent_main_sku(item):
    rows=item.decision.get('trace',{}).get('online_rows',[])
    if not rows or any(not isinstance(r,dict) or r.get('key')!=item.key for r in rows):return None
    values={r.get('main_sku') for r in rows}
    return next(iter(values)) if len(values)==1 else None


def visible_batch(session,batch,actor):
    return actor.role == "admin" or (
        not is_erp_reconciliation(batch)
        and any(can_access(session,actor,i.key) for i in session.scalars(select(Item).where(Item.batch_id==batch.id)))
    )

def scoped_batch(session,batch,actor):
    value=batch_view(batch)
    if actor.role!="admin":
        rows=[i for i in session.scalars(select(Item).where(Item.batch_id==batch.id)) if can_access(session,actor,i.key)]
        value['summary']=dict(Counter(i.status for i in rows));value['error']=None
    elif source_issues := batch.snapshot.get("source_issues"):
        value["source_issue_count"] = len(source_issues)
    return value

def job_visible(session,job,actor):
    # Batch jobs can contain unrelated errors/counts. Only an explicit owned item or correction is visible.
    item_id=job.payload.get('item_id')
    if job.kind=='correction':
        from app.models.coordination import Correction
        c=session.get(Correction,job.payload.get('correction_id'))
        item_id=c.item_id if c else None
    item=session.get(Item,item_id) if item_id else None
    return bool(item and can_view_item(session,actor,item))


@router.get("/health")
def health(request: Request):
    return {"status": "ok", "mode": request.app.state.config.adapter_mode}


@router.post("/auth/login")
def login(body: Login, request: Request, response: Response):
    config = request.app.state.config
    address = request.client.host if request.client else "local"
    now = time.monotonic()
    attempts = request.app.state.login_attempts
    # 本服务只有管理员登录，限制短时密码尝试；未持久化原始密码或请求正文。
    recent = [t for t in attempts.get(address, []) if now - t < 300]
    if len(recent) >= 10:
        raise HTTPException(429, "尝试次数过多，请5分钟后再试")
    user=None
    if body.username == config.admin_username and password_matches(body.password, config.admin_password):
        token=make_session(body.username,config.session_secret)
        result={"id":"admin","username":body.username,"display_name":"管理员","role":"admin","can_correct":True}
    else:
        with request.app.state.factory() as session:
            user=session.scalar(select(UserAccount).where(UserAccount.username==body.username))
            if not user or not user.active or not verify_password(body.password,user.password_hash):
                attempts[address]=recent+[now]
                raise HTTPException(401,"账号或密码不正确")
            token=make_session(user.username,config.session_secret,user.id,user.session_version)
            result=asdict(principal(user))
    attempts.pop(address,None)
    response.set_cookie(COOKIE,token,httponly=True,secure=config.secure_cookie,samesite="strict",max_age=28800,path="/")
    return result



@secured.get("/auth/me")
def me(user=Depends(require_user)):
    return asdict(user)


@secured.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@secured.get("/overview")
def overview(request: Request, actor=Depends(require_user)):
    state = request.app.state
    with state.factory() as session:
        counts = {name: 0 for name in ["本轮不调", "待处理", "待核验", "已核验"]}
        visible=[i for i in current_items(session) if can_access(session,actor,i.key)]
        if actor.role == "admin":
            visible_batch_ids = set(session.scalars(select(Batch.id).where(
                ~Batch.run_key.startswith(ERP_RECONCILIATION_PREFIX))))
        else:
            visible_batch_ids = {i.batch_id for i in session.scalars(select(Item)) if can_view_item(session, actor, i)}
        counts.update(Counter(item.status for item in visible))
        latest = session.scalar(select(Batch).where(Batch.id.in_(visible_batch_ids)).order_by(Batch.created_at.desc()).limit(1)) if visible_batch_ids else None
        worker = session.get(WorkerLease, "worker")
        today = utcnow().astimezone(BEIJING)
        delayed = today.hour >= 11 and (latest is None or latest.business_date != today.date().isoformat())
        return {"mode": state.config.adapter_mode, "counts": counts, "batch_count": len(visible_batch_ids),
                "latest_batch": scoped_batch(session,latest,actor) if latest else None, "worker": columns(worker) if worker and actor.role=="admin" else {"heartbeat": None, "last_error": None},
                "settings": settings_value(state.factory) if actor.role=="admin" else {}, "readiness": {**state.adapter.readiness(), "live_enabled": state.config.live_submit_enabled} if actor.role=="admin" else {},
                "delayed": delayed}


@secured.get("/batches")
def batches(request: Request, actor=Depends(require_user)):
    with request.app.state.factory() as session:
        return [scoped_batch(session,b,actor) for b in session.scalars(select(Batch).order_by(Batch.created_at.desc())) if visible_batch(session,b,actor)][:200]


@secured.post("/batches/run", dependencies=[Depends(require_admin)])
def run(request: Request):
    return {"job_id": enqueue(request.app.state.factory, "run", {})}


@secured.post("/demo/run", dependencies=[Depends(require_admin)])
def demo(request: Request):
    if request.app.state.config.adapter_mode != "demo":
        raise HTTPException(409, "只有演示环境可运行演示批次")
    state = request.app.state
    sites = ["MY", "PH", "SG", "TH", "VN"]
    pilot_shops = [f"DEMO-{site}" for site in sites]
    if state.config.coordination_required:
        snapshot = state.adapter.collect(utcnow().astimezone(BEIJING).date())
        # 显式演示入口仅为样例店铺补齐缺失归属，保留已有归属与人工控制。
        ensure_demo_ownership(state.factory, [
            row.key for row in snapshot.online
            if row.key.site in sites and row.key.shop_id == f"DEMO-{row.key.site}"
        ])
    update_settings(state.factory, {"autonomous_enabled": True, "pilot_shops": pilot_shops, "site_enabled": sites})
    return {"job_id": enqueue(state.factory, "run", {})}


@secured.get("/batches/{batch_id}")
def batch_detail(batch_id: str, request: Request, actor=Depends(require_user)):
    with request.app.state.factory() as session:
        batch = session.get(Batch, batch_id)
        if not batch or not visible_batch(session,batch,actor):
            raise HTTPException(404, "批次不存在")
        return {**scoped_batch(session,batch,actor), "items": [item_view(session,i,actor) for i in session.scalars(select(Item).where(Item.batch_id==batch_id)) if can_access(session,actor,i.key)]}


@secured.get("/batches/{batch_id}/source-issues", dependencies=[Depends(require_admin)])
def batch_source_issues(batch_id: str, request: Request,
                        offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
    with request.app.state.factory() as session:
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise HTTPException(404, "批次不存在")
        issues = batch.snapshot.get("source_issues", [])
        return {"total": len(issues), "items": issues[offset:offset + limit]}


@secured.get("/items")
def items(request: Request, status: str | None = None, batch_id: str | None = None, actor=Depends(require_user)):
    with request.app.state.factory() as session:
        if batch_id:
            query = select(Item).where(Item.batch_id == batch_id).order_by(Item.updated_at.desc(), Item.id.desc())
            if status:
                query = query.where(Item.status == status)
            rows = session.scalars(query).all()
        else:
            rows = [row for row in current_items(session) if not status or row.status == status]
        return [item_view(session,row,actor) for row in rows if can_view_item(session,actor,row)][:1000]


@secured.get("/items/{item_id}")
def item_detail(item_id: str, request: Request, actor=Depends(require_user)):
    with request.app.state.factory() as session:
        item = session.get(Item, item_id)
        if not item or not can_view_item(session,actor,item):
            raise HTTPException(404, "记录不存在")
        return item_view(session,item,actor)


@secured.post("/items/{item_id}/reevaluate", dependencies=[Depends(require_admin)])
def reevaluate(item_id: str, request: Request, body: Reevaluate = Reevaluate()):
    with transaction(request.app.state.factory) as session:
        item = session.get(Item, item_id)
        if not item:
            raise HTTPException(404, "记录不存在")
        if is_erp_reconciliation(session.get(Batch, item.batch_id)):
            raise HTTPException(409, "文件对账批次仅供查阅，请从来源生成新报告")
        session.add(Audit(id=str(uuid4()), action="item.reevaluate", actor=request.app.state.config.admin_username,
            detail={"item_id": item_id, "note": body.note}, created_at=stamp()))
    return {"job_id": enqueue(request.app.state.factory, "run", {"item_id": item_id})}


@secured.post("/reconcile", dependencies=[Depends(require_admin)])
def reconcile(request: Request):
    return {"job_id": enqueue(request.app.state.factory, "reconcile", {})}


@secured.get("/jobs/{job_id}")
def job_detail(job_id: str, request: Request, actor=Depends(require_user)):
    with request.app.state.factory() as session:
        job = session.get(Job, job_id)
        if not job or (actor.role!="admin" and not job_visible(session,job,actor)):
            raise HTTPException(404, "任务不存在")
        return {k: v for k, v in columns(job).items() if k not in {"owner", "dedupe_key", "payload"}}


@secured.get("/settings", dependencies=[Depends(require_admin)])
def get_runtime_settings(request: Request):
    return settings_value(request.app.state.factory)


@secured.patch("/settings", dependencies=[Depends(require_admin)])
def patch_settings(body: Changes, request: Request):
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "pilot_shops" in changes:
        changes["pilot_shops"] = sorted({s.strip() for s in changes["pilot_shops"] if s.strip()})
    if request.app.state.config.adapter_mode == "live" and changes.get("autonomous_enabled"):
        if not request.app.state.config.live_submit_enabled or not request.app.state.adapter.readiness().get("contract_ready"):
            raise HTTPException(409, "真实数据源待接入；配置与合同就绪后才可启用真实自动执行")
    return update_settings(request.app.state.factory, changes)


@secured.get("/protections")
def get_protections(request: Request, actor=Depends(require_user)):
    with request.app.state.factory() as session:
        return [columns(p) for p in session.scalars(select(Protection).where(Protection.active.is_(True)).order_by(Protection.created_at.desc())) if actor.role=="admin" or any(i.key.get("shop_id")==p.shop_id and i.key.get("site")==p.site and consistent_main_sku(i)==p.main_sku and can_view_item(session,actor,i) for i in session.scalars(select(Item)))]


@secured.post("/protections", dependencies=[Depends(require_admin)])
def add_protection(body: Protect, request: Request):
    with transaction(request.app.state.factory) as session:
        p = Protection(id=str(uuid4()), **body.model_dump(), active=True, created_at=stamp())
        session.add(p)
    return columns(p)


@secured.delete("/protections/{protection_id}", dependencies=[Depends(require_admin)])
def remove_protection(protection_id: str, request: Request):
    with transaction(request.app.state.factory) as session:
        protection = session.get(Protection, protection_id)
        if not protection:
            raise HTTPException(404, "保护记录不存在")
        protection.active = False
    return {"ok": True}


@secured.get("/contracts", dependencies=[Depends(require_admin)])
def contracts(request: Request):
    return {"mode": request.app.state.config.adapter_mode, **request.app.state.adapter.readiness()}


router.include_router(secured)
