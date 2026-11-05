from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.engine import allowed_error, evaluate, sales_window
from app.domain.types import FeeModel, OnlineState, SiteContract, Snapshot, SourceLine, VariantKey


NOW = datetime(2026, 11, 5, 0, 15, tzinfo=timezone.utc)
BUSINESS_DAY = date(2026, 11, 5)
START = date(2026, 10, 29)
END = date(2026, 11, 4)
SOURCES = ["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"]


def key(**overrides):
    return VariantKey(**{
        "platform": "TK", "shop_id": "01", "site": "MY", "product_id": "001",
        "item_id": "0001", "sku": "A", "warehouse": "仓一", **overrides,
    })


def line(variant, **overrides):
    return SourceLine(**{
        "row_id": "001", "order_id": "0001", "source": "loss_allocatable",
        "key": variant, "main_sku": "主款", "quantity": "1", "loss_amount": "-1.2",
        "currency": "MYR", "basis": "unit", **overrides,
    })


def online(variant, **overrides):
    return OnlineState(**{
        "key": variant, "main_sku": "主款", "currency": "MYR", "retail_price": "10",
        "marketing_price": None, "current_margin": "-1", "fee_scope": "normal",
        "fee_model": FeeModel(revenue_factor="1", fixed_cost="10", version="f1", verified=True),
        "observed_at": NOW, "sales_7d": 20, "sales_start": START, "sales_end": END,
        **overrides,
    })


def contract(site="MY", **overrides):
    return SiteContract(**{
        "site": site, "verified": True, "price_step": "0.01",
        "rounding": "ROUND_HALF_UP", "upload_column": "新零售价", "version": "c1",
        **overrides,
    })


def snapshot(lines, states, **overrides):
    return Snapshot(**{
        "as_of": NOW, "business_date": BUSINESS_DAY, "fx_date": BUSINESS_DAY,
        "fx_rates": {"CNY": "1", "MYR": "0.6", "VND": "3500"},
        "lines": lines, "online": states, "contracts": {"MY": contract()},
        "complete_sources": SOURCES, "source_versions": {"loss_allocatable": "erp-v1"},
        **overrides,
    })


def single(*, line_fields=None, online_fields=None, snapshot_fields=None):
    variant = key()
    rows = [line(variant, **(line_fields or {}))]
    states = [online(variant, **(online_fields or {}))]
    return evaluate(snapshot(rows, states, **(snapshot_fields or {})), NOW)[0]


def test_two_child_skus_have_separate_minimums():
    first = key()
    second = key(sku="B", item_id="0002")
    rows = [
        line(first, row_id="001", loss_amount="-1.2"),
        line(first, row_id="002", loss_amount="-1.5"),
        line(second, row_id="003", loss_amount="-2"),
        line(second, row_id="004", loss_amount="-3"),
    ]
    results = {item.key.sku: item for item in evaluate(snapshot(rows, [online(first), online(second)]), NOW)}
    assert (results["A"].unit_loss, results["A"].upload_price) == (Decimal("1.2"), Decimal("11.20"))
    assert (results["B"].unit_loss, results["B"].upload_price) == (Decimal("2"), Decimal("12.00"))
    assert results["A"].selected_row_id == "001"
    assert results["A"].source_row_ids == ["001", "002"]
    assert results["A"].ready and results["A"].status == "待处理"


@pytest.mark.parametrize(("basis", "amount"), [("line", "-2"), ("order", "-2"), ("unit", "-1")])
def test_loss_is_normalized_to_one_sellable_unit(basis, amount):
    result = single(line_fields={"basis": basis, "quantity": "2", "loss_amount": amount})
    assert result.ready
    assert result.unit_loss == Decimal("1")
    assert result.target_actual == Decimal("11")


def test_untrusted_order_without_valid_loss_is_pending():
    result = single(line_fields={"basis": "order", "allocation_reliable": False})
    assert result.status == "待处理"
    assert not result.ready


def test_untrusted_order_and_abnormal_rows_cannot_win_minimum():
    variant = key()
    rows = [
        line(variant),
        line(variant, row_id="002", basis="order", allocation_reliable=False, loss_amount="-0.01"),
        line(variant, row_id="003", abnormal=True, loss_amount="-0.02"),
    ]
    result = evaluate(snapshot(rows, [online(variant)]), NOW)[0]
    assert result.ready
    assert result.unit_loss == Decimal("1.2")
    assert result.trace["excluded_source_rows"] == {
        "002": "unreliable_order_allocation", "003": "abnormal",
    }


def test_duplicate_from_second_source_keeps_provenance_without_double_counting():
    variant = key()
    rows = [
        line(variant, source_ref="export-1:15"),
        line(variant, source="blocked_allocatable", source_ref="export-2:7"),
        line(variant, row_id="002", loss_amount="-0.8"),
    ]
    result = evaluate(snapshot(rows, [online(variant)]), NOW)[0]
    assert result.ready
    assert result.unit_loss == Decimal("0.8")
    assert result.source_row_ids == ["001", "002"]
    assert {"export-1:15", "export-2:7"}.issubset({entry["source_ref"] for entry in result.trace["source_rows"]})


def test_conflicting_duplicate_row_blocks_only_affected_variant():
    first = key()
    second = key(sku="B", item_id="0002")
    rows = [line(first), line(first, source="blocked_pending", loss_amount="-2"), line(second)]
    results = {item.key.sku: item for item in evaluate(snapshot(rows, [online(first), online(second)]), NOW)}
    assert not results["A"].ready
    assert results["A"].status == "待处理"
    assert results["B"].ready


def test_same_sku_and_row_id_in_different_stores_are_independent():
    first = key()
    second = key(shop_id="02")
    rows = [line(first, loss_amount="-0.4"), line(second, loss_amount="-0.8")]
    results = {item.key.shop_id: item for item in evaluate(snapshot(rows, [online(first), online(second)]), NOW)}
    assert results["01"].ready and results["02"].ready
    assert results["01"].unit_loss == Decimal("0.4")
    assert results["02"].unit_loss == Decimal("0.8")


@pytest.mark.parametrize(("units", "status", "ready"), [(20, "待处理", True), (21, "本轮不调", False)])
def test_twenty_one_sales_is_the_protection_boundary(units, status, ready):
    result = single(online_fields={"sales_7d": units})
    assert (result.status, result.ready) == (status, ready)


@pytest.mark.parametrize(
    ("age", "status", "ready"),
    [
        (timedelta(hours=72) - timedelta(seconds=1), "本轮不调", False),
        (timedelta(hours=72), "待处理", True),
        (-timedelta(seconds=1), "待处理", False),
    ],
)
def test_confirmed_increase_age_boundary(age, status, ready):
    result = single(online_fields={"last_increase_at": NOW - age})
    assert (result.status, result.ready) == (status, ready)


@pytest.mark.parametrize("sources", [SOURCES[:-1], [*SOURCES, "unknown"], [*SOURCES, SOURCES[0]]])
def test_exactly_four_sources_are_required(sources):
    result = single(snapshot_fields={"complete_sources": sources})
    assert result.status == "待处理"
    assert not result.ready


@pytest.mark.parametrize(
    "online_fields",
    [
        {"history_known": False},
        {"pending_submission": True},
        {"activity_locked": True},
        {"sellable": False},
        {"sales_complete": False},
        {"sales_7d": None},
        {"sales_7d": -1},
        {"sales_start": date(2026, 10, 30)},
        {"sales_end": date(2026, 11, 3)},
        {"fee_model": None},
        {"fee_model": FeeModel(revenue_factor="1", fixed_cost="10", version="f1")},
    ],
)
def test_unavailable_online_conditions_are_pending(online_fields):
    result = single(online_fields=online_fields)
    assert result.status == "待处理"
    assert not result.ready


@pytest.mark.parametrize("online_fields", [{"protected": True}, {"current_margin": "0"}])
def test_protected_or_non_loss_margins_are_excluded(online_fields):
    result = single(online_fields=online_fields)
    assert result.status == "本轮不调"
    assert not result.ready


def test_sales_window_uses_shanghai_midnight_and_includes_yesterday():
    assert sales_window(datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)) == (START, END)


def test_previous_day_fx_blocks_even_when_amounts_are_already_station_currency():
    result = single(snapshot_fields={"fx_date": date(2026, 11, 4)})
    assert result.status == "待处理"
    assert not result.ready


@pytest.mark.parametrize("rates", [{"CNY": "1"}, {"CNY": "1", "MYR": "0"}, {"CNY": "2", "MYR": "0.6"}])
def test_invalid_exchange_rates_cannot_fall_back_to_identity(rates):
    result = single(snapshot_fields={"fx_rates": rates})
    assert result.status == "待处理"
    assert not result.ready


@pytest.mark.parametrize("site", ["MY", "PH", "SG", "TH", "VN"])
def test_online_currency_must_match_site_even_when_exchange_rate_exists(site):
    variant = key(site=site)
    batch = snapshot(
        [line(variant, currency="CNY", loss_amount="-2")],
        [online(variant, currency="CNY")],
        contracts={site: contract(site)},
    )
    result = evaluate(batch, NOW)[0]
    assert (result.status, result.ready, result.upload_price) == ("待处理", False, None)


@pytest.mark.parametrize(
    ("site", "currency", "rate", "station_loss", "upload_price"),
    [
        ("MY", "MYR", "0.6", "1.2", "11.20"),
        ("PH", "PHP", "8", "16", "26.00"),
        ("SG", "SGD", "0.2", "0.4", "10.40"),
        ("TH", "THB", "5", "10", "20.00"),
        ("VN", "VND", "3500", "7000", "7010.00"),
    ],
)
def test_cny_loss_uses_units_per_cny_in_each_valid_station_currency(
    site, currency, rate, station_loss, upload_price,
):
    variant = key(site=site)
    batch = snapshot(
        [line(variant, currency="CNY", loss_amount="-2")],
        [online(variant, currency=currency)],
        fx_rates={"CNY": "1", currency: rate},
        contracts={site: contract(site)},
    )
    result = evaluate(batch, NOW)[0]
    assert result.ready
    assert result.station_loss == Decimal(station_loss)
    assert result.upload_price == Decimal(upload_price)


def test_discount_uses_actual_over_retail_and_is_applied_after_upload_rounding():
    result = single(
        line_fields={"loss_amount": "-0.9"},
        online_fields={
            "retail_price": "20", "marketing_price": "9",
            "fee_model": FeeModel(revenue_factor="0.9", fixed_cost="8.5", version="f2", verified=True),
        },
    )
    assert result.ready
    assert (result.current_actual, result.discount, result.target_actual) == (
        Decimal("9"), Decimal("0.45"), Decimal("9.9"),
    )
    assert result.upload_price == Decimal("22.00")
    assert result.predicted_profit == Decimal("0.41")


@pytest.mark.parametrize("online_fields", [{"marketing_price": "0"}, {"marketing_price": "11"}, {"retail_price": "0"}])
def test_invalid_current_prices_are_pending(online_fields):
    result = single(online_fields=online_fields)
    assert result.status == "待处理"
    assert not result.ready


def test_fee_shortfall_does_not_trigger_a_larger_automatic_increase():
    result = single(online_fields={
        "fee_model": FeeModel(revenue_factor="1", fixed_cost="11.21", version="f1", verified=True),
    })
    assert result.status == "待处理"
    assert not result.ready
    assert result.target_actual == Decimal("11.2")
    assert result.predicted_profit == Decimal("-0.01")


def test_rounding_can_turn_nonnegative_target_profit_into_a_shortfall():
    result = single(
        line_fields={"loss_amount": "-1.209"},
        online_fields={
            "fee_model": FeeModel(revenue_factor="1", fixed_cost="11.205", version="f1", verified=True),
        },
        snapshot_fields={"contracts": {"MY": contract(rounding="ROUND_DOWN")}},
    )
    assert not result.ready
    assert result.upload_price == Decimal("11.20")
    assert result.predicted_profit == Decimal("-0.005")


@pytest.mark.parametrize(
    ("site", "currency", "step", "loss", "tolerance", "ready"),
    [
        ("MY", "MYR", "0.1", "-0.02", "0.02", True),
        ("MY", "MYR", "0.1", "-0.03", "0.02", False),
        ("VN", "VND", "5", "-2", "2", True),
        ("VN", "VND", "5", "-2.1", "2", False),
    ],
)
def test_rounded_effective_price_must_stay_within_station_tolerance(site, currency, step, loss, tolerance, ready):
    variant = key(site=site)
    rows = [line(variant, currency=currency, loss_amount=loss)]
    states = [online(variant, currency=currency)]
    selected = contract(site, price_step=step)
    result = evaluate(snapshot(rows, states, contracts={site: selected}), NOW)[0]
    assert allowed_error(site) == Decimal(tolerance)
    assert result.ready is ready
    assert result.upload_price == Decimal("10")


@pytest.mark.parametrize(
    "contracts",
    [{}, {"MY": contract(verified=False)}, {"MY": contract("VN")}, {"MY": contract(price_step="0")}],
)
def test_station_price_precision_requires_a_matching_verified_contract(contracts):
    result = single(snapshot_fields={"contracts": contracts})
    assert result.status == "待处理"
    assert not result.ready


def test_round_up_is_applied_to_a_non_decimal_place_step():
    result = single(
        line_fields={"loss_amount": "-1.201"},
        snapshot_fields={"contracts": {"MY": contract(price_step="0.025", rounding="ROUND_UP")}},
    )
    assert result.upload_price == Decimal("11.225")
    assert not result.ready


@pytest.mark.parametrize("change", [{"fee_scope": "special"}, {"currency": "CNY"}, {"main_sku": "另一主款"}])
def test_conflicting_data_for_one_variant_is_pending(change):
    variant = key()
    rows = [line(variant), line(variant, row_id="002", **change)]
    result = evaluate(snapshot(rows, [online(variant)]), NOW)[0]
    assert result.status == "待处理"
    assert not result.ready


def test_warehouse_changes_group_but_not_identity_and_conflict_blocks():
    first = key()
    second = key(warehouse="仓二")
    assert first.identity() == second.identity()
    assert first.grouping() != second.grouping()
    assert len(first.identity()) == 64
    rows = [line(first), line(second, row_id="002")]
    results = evaluate(snapshot(rows, [online(first)]), NOW)
    assert len(results) == 1
    assert results[0].status == "待处理"
    assert not results[0].ready


def test_missing_online_mapping_is_pending():
    result = evaluate(snapshot([line(key())], []), NOW)[0]
    assert result.status == "待处理"
    assert not result.ready


@pytest.mark.parametrize(
    "fields",
    [{"row_id": ""}, {"row_id": "  "}, {"quantity": "0"}, {"main_sku": ""}, {"currency": ""}],
)
def test_missing_source_facts_are_pending(fields):
    result = single(line_fields=fields)
    assert result.status == "待处理"
    assert not result.ready


def test_missing_key_component_is_pending():
    variant = key(item_id="")
    result = evaluate(snapshot([line(variant)], [online(variant)]), NOW)[0]
    assert result.status == "待处理"
    assert not result.ready


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_loss_is_rejected(amount):
    with pytest.raises(ValidationError):
        line(key(), loss_amount=amount)


def test_numeric_id_is_rejected_instead_of_losing_leading_zeroes():
    with pytest.raises(ValidationError):
        key(item_id=1)


def test_naive_observed_at_is_rejected():
    with pytest.raises(ValidationError):
        online(key(), observed_at=NOW.replace(tzinfo=None))
