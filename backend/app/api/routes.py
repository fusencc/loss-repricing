from collections import Counter
from datetime import datetime
import time
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from app.models import Batch, Item, Execution, Job, Protection, Audit, WorkerLease
from app.db import transaction
from app.services.store import settings_value, update_settings, enqueue, utcnow, stamp, BEIJING, current_items
from app.api.auth import COOKIE, require_admin, make_session, password_matches

router = APIRouter(prefix="/api")
secured = APIRouter(dependencies=[Depends(require_admin)])


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
    return {k: v for k, v in columns(batch).items() if k not in {"snapshot", "run_key"}}


def item_view(session, item):
    value = columns(item)
    from app.domain.types import VariantKey
    value["target_id"] = VariantKey.model_validate(item.key).target_identity()
    execution = session.scalar(select(Execution).where(Execution.item_id == item.id))
    value["execution"] = columns(execution) if execution else None
    value["evidence"] = execution.evidence if execution else {}
    return value


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
    if body.username != config.admin_username or not password_matches(body.password, config.admin_password):
        attempts[address] = recent + [now]
        raise HTTPException(401, "账号或密码不正确")
    attempts.pop(address, None)
    response.set_cookie(COOKIE, make_session(body.username, config.session_secret), httponly=True,
                        secure=config.secure_cookie, samesite="strict", max_age=28800, path="/")
    return {"username": body.username}


@secured.get("/auth/me")
def me(user=Depends(require_admin)):
    return {"username": user}


@secured.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@secured.get("/overview")
def overview(request: Request):
    state = request.app.state
    with state.factory() as session:
        counts = {name: 0 for name in ["本轮不调", "待处理", "待核验", "已核验"]}
        counts.update(Counter(item.status for item in current_items(session)))
        latest = session.scalar(select(Batch).order_by(Batch.created_at.desc()).limit(1))
        worker = session.get(WorkerLease, "worker")
        today = utcnow().astimezone(BEIJING)
        delayed = today.hour >= 11 and (latest is None or latest.business_date != today.date().isoformat())
        return {"mode": state.config.adapter_mode, "counts": counts, "batch_count": session.scalar(select(func.count()).select_from(Batch)),
                "latest_batch": batch_view(latest) if latest else None, "worker": columns(worker) if worker else {"heartbeat": None, "last_error": None},
                "settings": settings_value(state.factory), "readiness": {**state.adapter.readiness(), "live_enabled": state.config.live_submit_enabled},
                "delayed": delayed}


@secured.get("/batches")
def batches(request: Request):
    with request.app.state.factory() as session:
        return [batch_view(b) for b in session.scalars(select(Batch).order_by(Batch.created_at.desc()).limit(200))]


@secured.post("/batches/run")
def run(request: Request):
    return {"job_id": enqueue(request.app.state.factory, "run", {})}


@secured.post("/demo/run")
def demo(request: Request):
    if request.app.state.config.adapter_mode != "demo":
        raise HTTPException(409, "只有演示环境可运行演示批次")
    # 显式演示按钮仅启用虚拟店铺；不会修改任何真实环境或真实店铺设置。
    update_settings(request.app.state.factory, {"autonomous_enabled": True,
        "pilot_shops": [f"DEMO-{site}" for site in ["MY", "PH", "SG", "TH", "VN"]],
        "site_enabled": ["MY", "PH", "SG", "TH", "VN"]})
    return {"job_id": enqueue(request.app.state.factory, "run", {})}


@secured.get("/batches/{batch_id}")
def batch_detail(batch_id: str, request: Request):
    with request.app.state.factory() as session:
        batch = session.get(Batch, batch_id)
        if not batch:
            raise HTTPException(404, "批次不存在")
        return {**batch_view(batch), "items": [item_view(session, i) for i in session.scalars(select(Item).where(Item.batch_id == batch_id))]}


@secured.get("/items")
def items(request: Request, status: str | None = None, batch_id: str | None = None):
    with request.app.state.factory() as session:
        if batch_id:
            query = select(Item).where(Item.batch_id == batch_id).order_by(Item.updated_at.desc(), Item.id.desc())
            if status:
                query = query.where(Item.status == status)
            rows = session.scalars(query).all()
        else:
            rows = [row for row in current_items(session) if not status or row.status == status]
        return [item_view(session, row) for row in rows[:1000]]


@secured.get("/items/{item_id}")
def item_detail(item_id: str, request: Request):
    with request.app.state.factory() as session:
        item = session.get(Item, item_id)
        if not item:
            raise HTTPException(404, "记录不存在")
        return item_view(session, item)


@secured.post("/items/{item_id}/reevaluate")
def reevaluate(item_id: str, request: Request, body: Reevaluate = Reevaluate()):
    with transaction(request.app.state.factory) as session:
        if not session.get(Item, item_id):
            raise HTTPException(404, "记录不存在")
        session.add(Audit(id=str(uuid4()), action="item.reevaluate", actor=request.app.state.config.admin_username,
            detail={"item_id": item_id, "note": body.note}, created_at=stamp()))
    return {"job_id": enqueue(request.app.state.factory, "run", {"item_id": item_id})}


@secured.post("/reconcile")
def reconcile(request: Request):
    return {"job_id": enqueue(request.app.state.factory, "reconcile", {})}


@secured.get("/jobs/{job_id}")
def job_detail(job_id: str, request: Request):
    with request.app.state.factory() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return {k: v for k, v in columns(job).items() if k not in {"owner", "dedupe_key", "payload"}}


@secured.get("/settings")
def get_runtime_settings(request: Request):
    return settings_value(request.app.state.factory)


@secured.patch("/settings")
def patch_settings(body: Changes, request: Request):
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "pilot_shops" in changes:
        changes["pilot_shops"] = sorted({s.strip() for s in changes["pilot_shops"] if s.strip()})
    if request.app.state.config.adapter_mode == "live" and changes.get("autonomous_enabled"):
        if not request.app.state.config.live_submit_enabled or not request.app.state.adapter.readiness().get("contract_ready"):
            raise HTTPException(409, "真实数据源待接入；配置与合同就绪后才可启用真实自动执行")
    return update_settings(request.app.state.factory, changes)


@secured.get("/protections")
def get_protections(request: Request):
    with request.app.state.factory() as session:
        return [columns(p) for p in session.scalars(select(Protection).where(Protection.active.is_(True)).order_by(Protection.created_at.desc()))]


@secured.post("/protections")
def add_protection(body: Protect, request: Request):
    with transaction(request.app.state.factory) as session:
        p = Protection(id=str(uuid4()), **body.model_dump(), active=True, created_at=stamp())
        session.add(p)
    return columns(p)


@secured.delete("/protections/{protection_id}")
def remove_protection(protection_id: str, request: Request):
    with transaction(request.app.state.factory) as session:
        protection = session.get(Protection, protection_id)
        if not protection:
            raise HTTPException(404, "保护记录不存在")
        protection.active = False
    return {"ok": True}


@secured.get("/contracts")
def contracts(request: Request):
    return {"mode": request.app.state.config.adapter_mode, **request.app.state.adapter.readiness()}


router.include_router(secured)
