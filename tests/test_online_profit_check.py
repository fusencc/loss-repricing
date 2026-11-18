"""11/12 在线利润保本校验，手算预期，不访问真实系统。"""
from datetime import timedelta
from decimal import Decimal
import pytest
from app.domain.types import FeeModel, OnlineProfitCheck
from test_pricing import single, NOW, contract


def check(stock="0", mode="direct", factor="0.8", cost="10", **changes):
    return OnlineProfitCheck(overseas_stock=stock, mode=mode, department="DEPT",
        inventory_observed_at=NOW, source_ref="synthetic/inventory-and-department-fees",
        model=FeeModel(revenue_factor=factor, fixed_cost=cost, version="synthetic-online-v1", verified=True),
        **changes)


def test_negative_online_profit_raises_original_compensation_to_break_even():
    result=single(online_fields={"profit_check":check()},
        snapshot_fields={"contracts":{"MY":contract(rounding="ROUND_UP")}})
    assert result.ready
    assert result.upload_price==Decimal("12.50")  # 原补亏11.2，0.8P-10=0 => 12.5
    assert Decimal(result.trace["online_profit"]["rounded_margin"])==0
    assert result.trace["online_profit"]["initial_target"]=="11.2"
    assert result.trace["online_profit"]["adjusted"] is True


def test_discount_and_tick_are_applied_after_selected_profit_floor():
    result=single(online_fields={"retail_price":"20","marketing_price":"10",
        "profit_check":check(cost="10.001")},
        snapshot_fields={"contracts":{"MY":contract(rounding="ROUND_UP")}})
    assert result.ready and result.upload_price==Decimal("25.01")
    assert Decimal(result.trace["online_profit"]["rounded_margin"])>0


@pytest.mark.parametrize("changes",[{"profit_check":None},
    {"profit_check":check(stock="1",mode="direct")},
    {"profit_check":check(stock="0",mode="overseas")},
    {"profit_check":check(factor="0")},
    {"profit_check":check().model_copy(update={"inventory_observed_at":NOW-timedelta(days=1)})}])
def test_missing_conflicting_or_stale_profit_evidence_never_passes(changes):
    result=single(online_fields=changes)
    assert not result.ready and result.reason_code=="unverified_online_profit"


def test_overseas_stock_uses_only_overseas_formula():
    result=single(online_fields={"profit_check":check(stock="2",mode="overseas",factor="1",cost="10")})
    assert result.ready and result.upload_price==Decimal("11.20")
    assert result.trace["online_profit"]["mode"]=="overseas"


def test_rounding_down_cannot_make_profit_negative_again():
    result=single(online_fields={"profit_check":check(factor="1",cost="11.205")},
        snapshot_fields={"contracts":{"MY":contract(rounding="ROUND_DOWN")}})
    assert result.ready and result.upload_price==Decimal("11.21")



def test_manual_correction_cannot_use_normal_margin_to_bypass_online_loss():
    from test_pricing import key, online, line, snapshot
    from app.services.corrections import manual_decision
    k=key();o=online(k,profit_check=check())
    snap=snapshot([line(k)],[o])
    with pytest.raises(ValueError,match="在线利润率"):
        manual_decision(snap,o,Decimal("11.2"),"synthetic","C1",NOW)
    result=manual_decision(snap,o,Decimal("12.5"),"synthetic","C1",NOW)
    assert result.ready and result.target_actual==Decimal("12.5")


def test_missing_profit_evidence_only_blocks_the_affected_variant():
    from test_pricing import key, online, line, snapshot
    from app.domain.engine import evaluate
    a=key();b=key(item_id="0002",sku="B")
    snap=snapshot([line(a),line(b,row_id="r2",order_id="o2")],
        [online(a,profit_check=None),online(b,profit_check=check())])
    results={d.key.sku:d for d in evaluate(snap,NOW)}
    assert not results["A"].ready and results["B"].ready


def test_zero_price_after_rounding_is_rejected_without_dividing_by_zero():
    result=single(line_fields={"loss_amount":"-0.001"},
        online_fields={"retail_price":"0.001", "profit_check":check(factor="1",cost="0"),
            },
        snapshot_fields={"contracts":{"MY":contract(rounding="ROUND_DOWN")}})
    assert not result.ready and result.reason_code=="rounding_error"
    assert result.trace["online_profit"]["rounded_margin"] is None
