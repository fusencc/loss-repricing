"""持久任务和业务记录。时间统一保存UTC ISO文本，避免MySQL丢失时区。"""
from sqlalchemy import String, Text, JSON, Boolean, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models.notifications import Notice
from app.db import Base


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_key: Mapped[str] = mapped_column(String(128), unique=True)
    business_date: Mapped[str] = mapped_column(String(10))
    mode: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40))
    snapshot: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("batch_id", "variant_id", name="uq_batch_variant"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"), index=True)
    variant_id: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    phase: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    decision: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String(40))


class Execution(Base):
    __tablename__ = "executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), unique=True)
    variant_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(24))
    request: Mapped[dict] = mapped_column(JSON)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


class CompanyPriceObservation(Base):
    """外部公司调价的实价观察，独立于本程序执行记录；首次确认时间不滚动。"""
    __tablename__ = 'company_price_observations'
    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    log_id: Mapped[str] = mapped_column(String(255), index=True)
    variant_id: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(24))
    currency: Mapped[str] = mapped_column(String(3))
    old_price: Mapped[str] = mapped_column(String(80))
    new_price: Mapped[str] = mapped_column(String(80))
    first_observed_at: Mapped[str] = mapped_column(String(40))
    first_confirmed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    logged_complete_at: Mapped[str] = mapped_column(String(40))
    company_context: Mapped[dict] = mapped_column(JSON)
    first_evidence: Mapped[dict] = mapped_column(JSON)
    confirmation_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))


class VariantGuard(Base):
    __tablename__ = "variant_guards"
    variant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_effective_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(20), default="queued")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Protection(Base):
    __tablename__ = "protections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(100))
    site: Mapped[str] = mapped_column(String(4))
    main_sku: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(40))


class Audit(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40))


class WorkerLease(Base):
    __tablename__ = "worker_lease"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36))
    expires: Mapped[float] = mapped_column(Float)
    heartbeat: Mapped[str] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

from .coordination import UserAccount, Ownership, TargetControl, Correction
