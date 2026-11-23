"""按 BD 项目 AU/AV 的字段手算，独立核对当前程序的利润率与利润额。"""
import json
from decimal import Decimal, localcontext

import pytest

from app.adapters.sales_fee_model import compile_direct_fee, compile_overseas_fee
from app.domain.money import MoneyRatio
from app.domain.types import FeeModel


def test_direct_bd_margin_with_vat_and_weight_steps():
    variant = {"currency": "PHP", "overseasStockSum": 0, "localWeight": "25", "costPrice": "4"}
    rate = {
        "version": "test-direct", "vatRate": "10", "fixedFreight": "0",
        "packageFee": "1", "warehouseCost": "2",
        "procedureRate": "2", "commissionRate": "8", "returnRate": "4",
        "marketingRate": "5", "otherRate": "0.5",
        "freight": {"base_weight_g": "10", "base_price": "3", "step_weight_g": "10", "step_price": "1.5"},
    }
    model = compile_direct_fee(variant, rate, site="PH", company_cny_per_currency="0.5")["online_model"]
    result = model.evaluate(MoneyRatio(Decimal("40")))

    # AU 去掉 M：25g 运费为 3+2×1.5=6 PHP。税后成本=(4+1+2+6×0.5)×1.1÷0.5=22 PHP。
    # 1−22÷40−(2+8+4+5+0.5)%=25.5%；每件利润为 40×25.5%=10.20 PHP。
    assert result.margin == MoneyRatio(Decimal("0.255"))
    assert result.profit == MoneyRatio(Decimal("10.20"))
    assert result.meets_minimum_margin


def test_overseas_bd_margin_with_current_warehouse_cost_and_vat():
    stock = [{"overseasWarehouses": ["MY-A"], "quantity": 3,
              "jqCostpriceList": ["12"], "jqFreightList": ["1.5"]}]
    variant = {"currency": "MYR", "overseasStockSum": 3, "marketPrice": "18",
               "overseasStockWarehouseInfo": json.dumps(stock)}
    rate = {
        "version": "test-overseas", "siteCode": "MY", "vatRate": "5",
        "costOfCapital": "1", "procedureRate": "2", "platformRate": "8", "returnRate": "3",
        "warehouseStorageRate": "0.5", "marketingRate": "4", "otherRate": "1.5", "totalRate": "20",
        "packageFee": "1", "warehouseCost": "0.75", "warehouseCostDomestic": "1.25",
        "overseaPackageFee": "0.60", "publishFee": "0.40", "dischargeFee": "1.1",
        "orderProcessFee": "1.4", "totalCost": "6.5",
    }
    model = compile_overseas_fee(
        variant, rate, site="MY", warehouse="MY-A", company_cny_per_currency="1.5"
    )["online_model"]
    result = model.evaluate(MoneyRatio(Decimal("20")))

    # AV 去掉 M：七项作业/包材合计 6.5，加采购 12、头程 1.5。含税成本=20×1.05÷1.5=14 MYR。
    # 1−14÷20−(1+2+8+3+0.5+4+1.5)%=10%；20 马币营销价每件赚 2 马币。
    assert result.margin == MoneyRatio(Decimal("0.10"))
    assert result.profit == MoneyRatio(Decimal("2"))
    assert result.meets_minimum_margin


@pytest.mark.parametrize(("price", "profit"), [("0", "-10"), ("-5", "-14")])
def test_nonpositive_price_retains_profit_without_a_margin(price, profit):
    model = FeeModel(revenue_factor="0.8", fixed_cost="10", version="test")
    result = model.evaluate(MoneyRatio(Decimal(price)))
    assert result.margin is None
    assert result.profit == MoneyRatio(Decimal(profit))
    assert not result.meets_minimum_margin


def test_recurring_price_at_and_below_one_percent_uses_exact_ratio():
    model = FeeModel(revenue_factor="0.8", fixed_cost="10", version="test")
    with localcontext() as context:
        context.prec = 4
        exact_floor = MoneyRatio(Decimal("1000"), Decimal("79"))
        at_floor = model.evaluate(exact_floor)
        below_floor = model.evaluate(exact_floor - MoneyRatio(Decimal("1e-55")))
    assert at_floor.margin == MoneyRatio(Decimal("0.01"))
    assert at_floor.profit == MoneyRatio(Decimal("10"), Decimal("79"))
    assert at_floor.meets_minimum_margin
    assert not below_floor.meets_minimum_margin
