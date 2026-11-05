"""可重复演示数据与持久模拟回执，仅在demo环境使用，绝不访问业务系统。"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import json
from hashlib import sha256
from app.domain.types import Snapshot, VariantKey, SourceLine, OnlineState, FeeModel, SiteContract
from app.adapters.base import SubmitResult, LookupResult, SourceUnavailable


class DemoAdapter:
    mode = "demo"
    timezone = timezone(timedelta(hours=8))

    def __init__(self, path):
        self.path = Path(path) / "demo-receipts"
        self.path.mkdir(parents=True, exist_ok=True)

    def readiness(self):
        return {"contract_ready": False, "missing": ["当前为模拟环境；未连接真实ERP和销售系统"],
                "verified_sites": [], "source_versions": {"demo": "synthetic-v1"}}

    def collect(self, business_date):
        now = datetime.now(timezone.utc)
        if business_date != now.astimezone(self.timezone).date():
            raise SourceUnavailable("演示来源只提供当日快照")
        sources = ["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"]
        lines, online = [], []
        # 固定的手工可验样本：MY售价10，亏损1，折扣0.5，上传22，毛利11*0.9-9.5=0.4。
        for index, (site, currency, scenario) in enumerate([
            ("MY", "MYR", "normal"), ("PH", "PHP", "normal"), ("SG", "SGD", "protected"),
            ("TH", "THB", "activity"), ("VN", "VND", "normal"), ("MY", "MYR", "history"),
            ("MY", "MYR", "profit"), ("SG", "SGD", "no_loss")
        ]):
            key = VariantKey(platform="TikTok", shop_id=f"DEMO-{site}", site=site,
                             product_id="DEMO-P100" if index == 6 else f"DEMO-P{index:03}",
                             item_id=f"DEMO-I{index:03}", sku=f"DEMO-SKU-{index:03}", warehouse="direct")
            price = Decimal("100000") if site == "VN" else Decimal("10")
            loss = Decimal("1000") if site == "VN" else Decimal("1")
            fixed = price * Decimal("0.9") + loss * Decimal("0.5")
            for repeat, multiplier in enumerate([1, 2]):
                lines.append(SourceLine(row_id=f"R{index}-{repeat}", order_id=f"O{index}-{repeat}",
                    source=sources[(index + repeat) % 4], key=key, main_sku=f"MAIN-{index}",
                    quantity=Decimal("1"), loss_amount=-loss * multiplier, currency=currency,
                    basis="unit", source_ref=f"synthetic-v1/row-{index}-{repeat}"))
            online.append(OnlineState(key=key, main_sku=f"MAIN-{index}", currency=currency,
                retail_price=price * 2, marketing_price=price, current_margin=Decimal("0.03") if scenario == "no_loss" else Decimal("-0.05"),
                fee_scope="normal", fee_model=FeeModel(revenue_factor=Decimal("0.9"), fixed_cost=fixed,
                                                      version="demo-no-marketing-v1", verified=True),
                observed_at=now, sales_7d=21 if scenario == "protected" else 6,
                sales_start=business_date - timedelta(days=7), sales_end=business_date - timedelta(days=1),
                history_known=scenario != "history", activity_locked=scenario == "activity"))
        return Snapshot(as_of=now, business_date=business_date, fx_date=business_date,
            fx_rates={"CNY": "1", "MYR": "0.6", "PHP": "8", "SGD": "0.2", "THB": "5", "VND": "3500"},
            lines=lines, online=online, complete_sources=sources,
            contracts={site: SiteContract(site=site, verified=True, price_step="1" if site == "VN" else "0.01",
                rounding="ROUND_HALF_UP", upload_column="税后价格", version="demo-only-v1") for site in ["MY", "PH", "SG", "TH", "VN"]},
            source_versions={"erp": "synthetic-v1", "sales": "synthetic-v1", "fees": "demo-no-marketing-v1"})

    def _receipt_path(self, execution_id):
        return self.path / (sha256(execution_id.encode()).hexdigest() + ".json")

    def submit(self, execution_id, decision):
        path = self._receipt_path(execution_id)
        if not path.exists():
            receipt = {"variant_id": decision.key.identity(), "actual_price": str(decision.upload_price * decision.discount),
                       "effective_at": datetime.now(timezone.utc).isoformat()}
            # 一个执行ID只创建一次回执；模拟外部系统的幂等键，而非复用本地批次状态。
            with path.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False)
        return SubmitResult("accepted", "demo-" + execution_id, "模拟受理，待价格回读")

    def lookup(self, execution_id, task_id, key):
        path = self._receipt_path(execution_id)
        if not path.exists():
            return LookupResult("unknown", message="未找到模拟回执，不推断为未提交")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["variant_id"] != key.identity():
            return LookupResult("unknown", message="回执商品身份不一致")
        return LookupResult("accepted", Decimal(data["actual_price"]), datetime.now(timezone.utc),
                            datetime.fromisoformat(data["effective_at"]), message="模拟实际售价证据")
