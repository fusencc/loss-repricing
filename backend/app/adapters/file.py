"""归一化JSON文件对账入口；不允许以文件来源冒充实时自动执行。"""
from app.adapters.base import SourceUnavailable, SubmitResult, LookupResult
from app.domain.types import Snapshot


class FileAdapter:
    mode = "file"
    def __init__(self, path):
        self.path = path

    def collect(self, business_date):
        if not self.path.exists():
            raise SourceUnavailable("未找到标准化快照文件")
        snapshot = Snapshot.model_validate_json(self.path.read_text(encoding="utf-8"))
        if snapshot.business_date != business_date:
            raise SourceUnavailable("文件业务日期不是当日，请更新来源")
        return snapshot

    def readiness(self):
        return {"contract_ready": False, "missing": ["文件模式仅用于对账，不允许真实自动提交"], "verified_sites": [], "source_versions": {}}

    def submit(self, execution_id, decision):
        return SubmitResult("rejected", message="文件对账模式不提交")

    def lookup(self, execution_id, task_id, key):
        return LookupResult("unknown", message="文件不能证明实际生效价")
