"""外部系统边界。受理不是成功，真实价格必须有同口径且较新的观测证据。"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, Literal
from app.domain.types import Snapshot, Decision, VariantKey


class SourceUnavailable(RuntimeError):
    """尚无可用的完整当日来源；可等待就绪，不能复用昨天文件。"""


@dataclass
class SubmitResult:
    status: Literal["accepted", "rejected", "unknown"]
    task_id: str | None = None
    message: str = ""


@dataclass
class LookupResult:
    status: Literal["accepted", "rejected", "unknown"]
    actual_price: Decimal | None = None
    observed_at: datetime | None = None
    effective_at: datetime | None = None
    price_basis: str = "actual_including_vat"
    message: str = ""
    # 独立价格口径及官方查询证据；actual_price继续只表示营销实际有效价。
    price_record: dict | None = None


class Adapter(Protocol):
    mode: str
    def collect(self, business_date: date) -> Snapshot: ...
    def submit(self, execution_id: str, decision: Decision) -> SubmitResult: ...
    def lookup(self, execution_id: str, task_id: str | None, key: VariantKey) -> LookupResult: ...
    def readiness(self) -> dict: ...
