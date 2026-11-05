"""Persistent recipient-specific outbox; business acknowledgment is separate from delivery."""
from sqlalchemy import String, Text, JSON, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class Notice(Base):
    __tablename__ = 'notices'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    recipient_id: Mapped[str] = mapped_column(String(36), index=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey('batches.id'), nullable=True)
    item_ids: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    delivery_evidence: Mapped[list] = mapped_column(JSON, default=list)
    delivery_state: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recipient_userid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ack_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ack_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    escalated_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

