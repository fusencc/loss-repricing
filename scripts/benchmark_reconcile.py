"""测量同一批次的大量模拟有效价回查。"""
import argparse
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

from sqlalchemy import func, select

from app.adapters.base import LookupResult
from app.db import Base, create_database
from app.domain.types import Decision, VariantKey
from app.models import Batch, Execution, Item, Notice, VariantGuard
from app.models.coordination import Ownership
from app.services.coordination import scope_key
from app.services.execution import PricingService
from app.services.store import stamp, utcnow


class Readback:
    mode = "demo"

    def __init__(self):
        self.lookups = 0

    def lookup(self, execution_id, task_id, key):
        self.lookups += 1
        return LookupResult("accepted", Decimal("11.00"), utcnow())

    def submit(self, *args):
        raise AssertionError("回查基准不能提交价格")


def run(size):
    with TemporaryDirectory() as directory:
        engine, factory = create_database(f"sqlite:///{Path(directory) / 'reconcile.sqlite3'}")
        Base.metadata.create_all(engine)
        now = stamp()
        key = VariantKey(platform="TikTok", shop_id="DEMO-MY", site="MY", product_id="P",
                         item_id="item", sku="SKU", warehouse="direct")
        with factory.begin() as session:
            session.add(Ownership(id="bench-owner", scope_key=scope_key(key.platform, key.shop_id, key.site),
                platform=key.platform, shop_id=key.shop_id, site=key.site, owner_id="admin", autonomous=True, version=1))
            session.add(Batch(id="bench", run_key="bench", business_date="2026-11-10", mode="demo",
                              status="pending_verification", created_at=now, snapshot={}, summary={}))
            session.flush()
            executions = []
            for number in range(size):
                target = key.model_copy(update={"item_id": str(number), "sku": f"SKU-{number}"})
                decision = Decision(key=target, status="待处理", ready=True, reason_code="ready",
                                    reason="合成样例", source_row_ids=[], target_actual="11.00", upload_price="22.00")
                item_id = f"item-{number}"
                identity = target.target_identity()
                execution_id = f"execution-{number}"
                session.add(Item(id=item_id, batch_id="bench", variant_id=target.identity(),
                                 key=target.model_dump(mode="json"), status="待核验", phase="unknown",
                                 reason="", decision=decision.model_dump(mode="json"), updated_at=now))
                session.add(VariantGuard(variant_id=identity, execution_id=execution_id))
                executions.append(Execution(id=execution_id, item_id=item_id, variant_id=identity, state="unknown",
                                            request=decision.model_dump(mode="json"), created_at=now, updated_at=now))
            session.flush()
            session.add_all(executions)

        adapter = Readback()
        service = PricingService(factory, adapter, SimpleNamespace(coordination_required=True))
        start = perf_counter()
        service.reconcile()
        elapsed = perf_counter() - start
        service.reconcile()
        with factory() as session:
            assert adapter.lookups == size
            assert session.scalar(select(func.count()).select_from(Execution).where(Execution.state == "verified")) == size
            assert not session.scalar(select(func.count()).select_from(VariantGuard).where(VariantGuard.execution_id != None))
            assert session.get(Batch, "bench").summary == {"已核验": size}
            notice = session.scalars(select(Notice)).one()
            assert notice.summary["statuses"] == {"已核验": size} and len(notice.item_ids) == size
        engine.dispose()
        print(f"{size} records: {elapsed:.3f} s, {size / elapsed:.1f} records/s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sizes", type=int, nargs="+")
    args = parser.parse_args()
    if any(size < 1 for size in args.sizes):
        parser.error("数量须为正整数")
    for size in args.sizes:
        run(size)
