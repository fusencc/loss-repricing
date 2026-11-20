"""直接补亏规范的独立算术对照。全部为合成数据，不验证公司费率或真实接口。

Fraction 仅作测试侧精确有理数参照，不调用生产舍入/换汇实现生成预期结果。
"""
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from app.domain.engine import FORMULA_VERSION, evaluate
from app.domain.types import FeeModel, OnlineProfitCheck, OnlineState, SiteContract, Snapshot, SourceLine, VariantKey


NOW = datetime(2026, 11, 5, 1, tzinfo=timezone.utc)
DAY = date(2026, 11, 5)


def sample(*, loss="-2", quantity="1", basis="unit", currency="CNY", current="10", retail="20",
           step="0.01", rounding="ROUND_HALF_UP", factor="0.9", cost="9.5"):
    key = VariantKey(platform="synthetic", shop_id="01", site="MY", product_id="001",
                     item_id="0001", sku="A", warehouse="direct")
    return Snapshot(
        as_of=NOW, business_date=DAY, fx_date=DAY,
        fx_rates={"CNY": "1", "MYR": "0.6", "USD": "0.15"},
        lines=[SourceLine(row_id="r1", order_id="o1", source="loss_allocatable", key=key,
                          main_sku="main", quantity=quantity, loss_amount=loss,
                          currency=currency, basis=basis, allocation_reliable=True)],
        online=[OnlineState(key=key, main_sku="main", currency="MYR", retail_price=retail,
                            marketing_price=current, fee_scope="normal",
                            profit_check=OnlineProfitCheck(overseas_stock="0",mode="direct",department="TEST",
                                inventory_observed_at=NOW,source_ref="synthetic/profit",
                                model=FeeModel(revenue_factor=factor,fixed_cost=cost,version="synthetic-online-v1",verified=True)),
                            observed_at=NOW, sales_7d=0, sales_complete=True, history_known=True,
                            sales_basis="erp_goods_warehouse_seven_days_reference",
                            sales_observed_at=NOW)],
        contracts={"MY": SiteContract(site="MY", verified=True, price_step=step, rounding=rounding,
                                      upload_column="税后价格", version="synthetic-v1", warehouses=["direct"])},
        complete_sources=["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"],
    )


def as_fraction(value):
    return Fraction(str(value))


def assert_close_decimal(actual, expected):
    # 生产50位有效数字产生的循环小数误差应远小于价格档位；参考值不经过float。
    assert abs(as_fraction(actual) - expected) < Fraction(1, 10**45)


def test_documented_synthetic_example_and_formula_provenance():
    snapshot = sample(loss="-1", currency="MYR")
    second = snapshot.lines[0].model_copy(update={"row_id": "r2", "loss_amount": Decimal("-2")})
    snapshot.lines.append(second)
    result = evaluate(snapshot, NOW)[0]
    assert result.ready and result.status == "待处理"
    assert result.selected_row_id == "r1"
    assert (result.unit_loss, result.discount, result.target_actual, result.upload_price,
            result.predicted_profit) == tuple(map(Decimal, ["1", "0.5", "11", "22", "0.4"]))
    assert result.trace["formula_version"] == FORMULA_VERSION == "loss-erp-corrected-margin-reference-priority-20261120-v13"
    assert result.trace["decimal_precision"] == 50


@pytest.mark.parametrize("rounding", ["ROUND_HALF_UP", "ROUND_DOWN", "ROUND_UP"])
@pytest.mark.parametrize("step", ["0.01", "0.025"])
def test_formula_matches_independent_rational_reference(rounding, step):
    for currency, from_rate in [("CNY", "1"), ("MYR", "0.6"), ("USD", "0.15")]:
        for current, retail in [("10", "10"), ("9", "20"), ("7", "13")]:
            for basis, quantity in [("unit", "2"), ("line", "3"), ("order", "3")]:
                snapshot = sample(loss="-1.237", currency=currency, current=current, retail=retail,
                                  basis=basis, quantity=quantity, step=step, rounding=rounding,
                                  factor="0.9", cost="6.4")
                result = evaluate(snapshot, NOW)[0]
                unit = Fraction("1.237") / (1 if basis == "unit" else Fraction(quantity))
                delta = unit * Fraction("0.6") / Fraction(from_rate)
                target = Fraction(current) + delta
                raw = target * Fraction(retail) / Fraction(current)
                ticks = raw / Fraction(step)
                whole, remainder = divmod(ticks.numerator, ticks.denominator)
                if rounding == "ROUND_UP":
                    whole += bool(remainder)
                elif rounding == "ROUND_HALF_UP":
                    whole += 2 * remainder >= ticks.denominator
                upload = whole * Fraction(step)
                actual = upload * Fraction(current) / Fraction(retail)
                target_profit = target * Fraction("0.9") - Fraction("6.4")
                profit = actual * Fraction("0.9") - Fraction("6.4")
                assert_close_decimal(result.unit_loss, unit)
                assert_close_decimal(result.station_loss, delta)
                assert_close_decimal(result.target_actual, target)
                assert as_fraction(result.upload_price) == upload
                assert_close_decimal(result.trace["rounded_actual"], actual)
                assert_close_decimal(result.predicted_profit, profit)
                assert result.ready == (abs(actual - target) <= Fraction("0.02")
                                        and target_profit >= target/100 and profit >= actual/100)


def test_external_decimal_context_cannot_change_pricing_result():
    snapshot = sample(loss="-1.237", basis="line", quantity="3", current="7", retail="13")
    expected = evaluate(snapshot, NOW)[0]
    with localcontext() as context:
        context.prec = 6
        context.rounding = "ROUND_DOWN"
        actual = evaluate(snapshot, NOW)[0]
    assert actual == expected


@pytest.mark.parametrize('rounding', ['ROUND_DOWN', 'ROUND_UP', 'ROUND_HALF_UP'])
def test_raw_company_fx_preserves_exact_tick_at_one_percent(rounding):
    # 来源是3 CNY/MYR。3 CNY亏损精确补1 MYR；显示倒数1/3不能回读作算价输入。
    snapshot = sample(loss='-3', currency='CNY', current='10', retail='10',
                      rounding=rounding, factor='1', cost='10.89')
    snapshot = Snapshot.model_validate({**snapshot.model_dump(),
        'fx_rates':{'CNY':'1','MYR':'3'}, 'fx_rate_unit':'cny_per_currency'})
    result = evaluate(snapshot, NOW)[0]
    assert result.ready
    assert result.target_actual == Decimal('11')
    assert result.upload_price == Decimal('11.00')
    assert result.predicted_profit == Decimal('0.11')
    assert result.trace['fx_rate_unit'] == 'cny_per_currency'


def test_raw_company_fx_converts_between_two_foreign_currencies():
    snapshot = sample(loss='-1.5', currency='USD', current='10', retail='10', factor='1', cost='11')
    snapshot = Snapshot.model_validate({**snapshot.model_dump(),
        'fx_rates':{'CNY':'1','MYR':'3','USD':'6'}, 'fx_rate_unit':'cny_per_currency'})
    result = evaluate(snapshot, NOW)[0]
    assert result.ready
    assert result.station_loss == Decimal('3')
    assert result.upload_price == Decimal('13.00')


@pytest.mark.parametrize(("cost", "upload"), [("9.79", "22"), ("9.790000000000000000000000000001", "22.01")])
def test_one_percent_boundary_requires_an_extra_tick_for_any_shortfall(cost, upload):
    result = evaluate(sample(loss="-1", currency="MYR", cost=cost), NOW)[0]
    assert result.ready
    assert result.upload_price == Decimal(upload)
    assert result.predicted_profit >= 0
    # 即使差额低于展示精度，也不能通过取整伪造达到1%。
    before = (Fraction(upload)-Fraction("0.01"))/2 * Fraction("0.89") - Fraction(cost)
    assert before < 0


@pytest.mark.parametrize(("loss", "current", "retail", "rounding", "cost", "upload"), [
    ("-1", "1", "3", "ROUND_DOWN", "1.1", "4.00"),
    ("-0.19", "0.04", "0.18", "ROUND_HALF_UP", "0.05", "0.47"),
])
def test_repeating_unit_loss_does_not_shift_exact_tick_or_half_tick(loss, current, retail, rounding, cost, upload):
    result = evaluate(sample(loss=loss, quantity="3", basis="line", currency="MYR",
                             current=current, retail=retail, rounding=rounding, factor="1", cost=cost), NOW)[0]
    assert result.ready
    assert result.upload_price == Decimal(upload)


def test_repeating_target_with_exact_one_percent_is_allowed():
    result = evaluate(sample(loss="-1", quantity="3", basis="line", currency="MYR",
                             current="10", retail="10", rounding="ROUND_UP", factor="0.91", cost="9.3"), NOW)[0]
    assert result.ready
    assert Decimal(result.trace["online_profit"]["target_margin"]) == Decimal(".01")
    assert result.upload_price == Decimal("10.34")
    assert result.predicted_profit == Decimal("0.1094")


def test_repeating_fx_preserves_exact_half_tick():
    snapshot = sample(loss="-0.19", quantity="1", currency="USD", current="0.04", retail="0.18",
                      rounding="ROUND_HALF_UP", factor="1", cost="0.05")
    snapshot.fx_rates["USD"] = Decimal("1.8")  # 0.19×0.6/1.8=0.19/3，与上面的半档样例等价。
    result = evaluate(snapshot, NOW)[0]
    assert result.ready and result.upload_price == Decimal("0.47")


def test_equivalent_exact_losses_tie_break_by_row_id():
    snapshot = sample(loss="-1", quantity="3", basis="line", currency="MYR")
    snapshot.lines[0].row_id = "z"
    snapshot.lines.append(snapshot.lines[0].model_copy(update={
        "row_id": "a", "loss_amount": Decimal("-2"), "quantity": Decimal("6"),
    }))
    assert evaluate(snapshot, NOW)[0].selected_row_id == "a"


@pytest.mark.parametrize(("section", "field", "reason"), [
    ("lines", "allocation_reliable", "no_valid_loss"),
])
def test_missing_confirmation_does_not_default_to_approval(section, field, reason):
    snapshot = sample(loss="-1", currency="MYR", basis="order")
    payload = snapshot.model_dump(mode="json")
    # 同批独立目标保持完整，证明未知标记没有扩大为整批停止。
    for name in ("lines", "online"):
        extra = snapshot.model_dump(mode="json")[name][0]
        extra["key"]["item_id"], extra["key"]["sku"] = "0002", "B"
        if name == "lines":
            extra["row_id"] = "r2"
            extra["order_id"] = "0002"  # 两个独立订单，各自只有一条可归属明细。
        payload[name].append(extra)
    del payload[section][0][field]
    results = evaluate(Snapshot.model_validate(payload), NOW)
    assert results[0].ready is False and results[0].reason_code == reason
    assert results[1].ready


def test_published_snapshot_schema_matches_confirmation_defaults():
    import json
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parents[1] / "docs" / "snapshot.schema.json").read_text(encoding="utf-8"))
    current = Snapshot.model_json_schema()
    for model, field in [("SourceLine", "allocation_reliable"), ("OnlineState", "sales_complete"),
                         ("OnlineState", "history_known")]:
        assert schema["$defs"][model]["properties"][field]["default"] is False
        assert schema["$defs"][model]["properties"][field] == current["$defs"][model]["properties"][field]
    assert schema["$defs"]["SiteContract"] == current["$defs"]["SiteContract"]
    assert schema["properties"]["source_issues"] == current["properties"]["source_issues"]
    for model in ("CandidateSourceIssue", "OnlineSourceIssue"):
        assert schema["$defs"][model] == current["$defs"][model]


@pytest.mark.parametrize("warehouses", [[], ["overseas"]])
def test_missing_or_wrong_contract_warehouse_blocks_only_affected_scope(warehouses):
    snapshot = sample(loss="-1", currency="MYR")
    snapshot.contracts["MY"].warehouses = warehouses
    result = evaluate(snapshot, NOW)[0]
    assert not result.ready and result.reason_code == "unsupported_warehouse"
