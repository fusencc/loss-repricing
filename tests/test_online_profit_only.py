"""11/12会议及11/17用户确认：补亏后仅校验库存对应完整在线利润。"""
from decimal import Decimal

import pytest

from app.domain.engine import evaluate
from app.domain.types import FeeModel
from app.services.corrections import manual_decision
from test_pricing import NOW, key, line, online, snapshot


def test_automatic_price_uses_only_full_online_profit():
    variant = key()
    state = online(variant)
    assert "fee_model" not in type(state).model_fields
    state.profit_check.model = FeeModel(revenue_factor='0.8', fixed_cost='10',
                                      version='full-online', verified=True)
    result = evaluate(snapshot([line(variant)], [state]), NOW)[0]
    assert result.ready
    # 完整在线利润 0.8P - 10；达到 1% 需 P >= 10/0.79，进档到 12.66。
    assert result.trace['online_profit']['target_margin'] == '0.01'
    assert result.upload_price == Decimal('12.66')
    assert result.predicted_profit == Decimal('0.128')


def test_manual_price_uses_only_full_online_profit():
    variant = key()
    state = online(variant)
    assert "fee_model" not in type(state).model_fields
    state.profit_check.model = FeeModel(revenue_factor='0.8', fixed_cost='10',
                                      version='full-online', verified=True)
    batch = snapshot([line(variant)], [state])
    result = manual_decision(batch, state, Decimal('12.66'), 'test', 'correction', NOW)
    assert result.ready and result.predicted_profit == Decimal('0.128')
    # 12.65 仍有正利润，但不到 1%，不能仅凭不亏放行。
    with pytest.raises(ValueError, match='在线利润率未达到1%'):
        manual_decision(batch, state, Decimal('12.65'), 'test', 'correction', NOW)
