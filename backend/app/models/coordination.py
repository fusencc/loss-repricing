"""身份、归属与人工控制。金额只存十进制文本。"""
from sqlalchemy import String, Text, JSON, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

class UserAccount(Base):
    __tablename__='user_accounts'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    username: Mapped[str]=mapped_column(String(100),unique=True)
    display_name: Mapped[str]=mapped_column(String(100))
    role: Mapped[str]=mapped_column(String(16))
    can_correct: Mapped[bool]=mapped_column(Boolean,default=False)
    active: Mapped[bool]=mapped_column(Boolean,default=True)
    password_hash: Mapped[str]=mapped_column(Text)
    session_version: Mapped[int]=mapped_column(Integer,default=1)
    dingtalk_userid: Mapped[str|None]=mapped_column(String(128),unique=True,nullable=True)
    created_at: Mapped[str]=mapped_column(String(40))
    updated_at: Mapped[str]=mapped_column(String(40))

class Ownership(Base):
    __tablename__='ownerships'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    scope_key: Mapped[str]=mapped_column(String(64),unique=True)
    platform: Mapped[str]=mapped_column(String(64))
    shop_id: Mapped[str]=mapped_column(String(100))
    site: Mapped[str]=mapped_column(String(4))
    target_id: Mapped[str|None]=mapped_column(String(64),nullable=True)
    owner_id: Mapped[str]=mapped_column(String(36))
    supervisor_id: Mapped[str|None]=mapped_column(String(36),nullable=True)
    autonomous: Mapped[bool]=mapped_column(Boolean,default=False)
    version: Mapped[int]=mapped_column(Integer,default=1)

class TargetControl(Base):
    __tablename__='target_controls'
    target_id: Mapped[str]=mapped_column(String(64),primary_key=True)
    key: Mapped[dict]=mapped_column(JSON)
    mode: Mapped[str]=mapped_column(String(16),default='auto')
    version: Mapped[int]=mapped_column(Integer,default=1)
    reason: Mapped[str]=mapped_column(Text,default='')
    changed_by: Mapped[str]=mapped_column(String(36),default='system')
    updated_at: Mapped[str]=mapped_column(String(40))
    baseline: Mapped[dict]=mapped_column(JSON,default=dict)
    handback_pending: Mapped[bool]=mapped_column(Boolean,default=False)

class Correction(Base):
    __tablename__='corrections'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    target_id: Mapped[str]=mapped_column(String(64),index=True)
    item_id: Mapped[str]=mapped_column(String(36))
    desired_actual: Mapped[str]=mapped_column(String(100))
    reason: Mapped[str]=mapped_column(Text)
    requested_by: Mapped[str]=mapped_column(String(36))
    state: Mapped[str]=mapped_column(String(20),default='proposed')
    control_version: Mapped[int]=mapped_column(Integer)
    baseline: Mapped[dict]=mapped_column(JSON)
    job_id: Mapped[str|None]=mapped_column(String(36),nullable=True)
    execution_id: Mapped[str|None]=mapped_column(String(36),nullable=True)
    created_at: Mapped[str]=mapped_column(String(40))
    updated_at: Mapped[str]=mapped_column(String(40))
    result: Mapped[dict]=mapped_column(JSON,default=dict)
    approved_by: Mapped[str|None]=mapped_column(String(36),nullable=True)
