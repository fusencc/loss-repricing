from decimal import Decimal
import pytest
from app.domain.money import MoneyRatio
from app.domain.types import FeeModel
from test_online_profit_check import check
from test_execution import context
from test_pricing import single, contract, NOW, key, online, line, snapshot
from app.services.corrections import manual_decision

@pytest.mark.parametrize('stock,mode', [('0','direct'),('2','overseas')])
def test_one_percent_discounted_upload(stock, mode):
    result=single(online_fields={'retail_price':'20','marketing_price':'10',
        'profit_check':check(stock=stock,mode=mode)},
        snapshot_fields={'contracts':{'MY':contract(rounding='ROUND_UP')}})
    assert result.ready
    assert result.upload_price == Decimal('25.32')
    assert result.trace['online_profit']['minimum_margin'] == '0.01'
    assert Decimal(result.trace['online_profit']['rounded_margin']) >= Decimal('.01')

@pytest.mark.parametrize('price,passed', [('100',True),('99.999999999999999999999999',False),('101',True)])
def test_exact_one_percent_boundary(price, passed):
    model=FeeModel(revenue_factor='.8',fixed_cost='79',version='test',verified=True)
    assert model.evaluate(MoneyRatio(Decimal(price))).meets_minimum_margin is passed

@pytest.mark.parametrize('factor',['0','.005','.01'])
def test_no_finite_threshold_price_is_blocked(factor):
    result=single(online_fields={'profit_check':check(factor=factor)})
    assert not result.ready
    assert result.reason_code=='unverified_online_profit'

@pytest.mark.parametrize('activity_locked', [False, True])
def test_manual_break_even_is_rejected_and_one_percent_passes(activity_locked):
    k=key();o=online(k,profit_check=check(),activity_locked=activity_locked);snap=snapshot([line(k)],[o])
    with pytest.raises(ValueError,match='在线利润率'):
        manual_decision(snap,o,Decimal('12.5'),'test','C1',NOW)
    assert manual_decision(snap,o,Decimal('12.66'),'test','C1',NOW).ready

def test_readback_under_one_percent_does_not_verify(context):
    from fake_adapter import DemoAdapter
    from app.services.execution import PricingService
    from app.services.store import settings_value
    from app.models import Execution
    from dataclasses import replace
    from test_execution import enable
    factory,cfg,_=context
    class BelowMargin(DemoAdapter):
        def collect(self, business_date):
            snap=super().collect(business_date)
            for row in snap.online:
                if row.key.site=='MY':
                    row.profit_check.model.revenue_factor=Decimal('.8')
                    row.profit_check.model.fixed_cost=Decimal('10')
            return snap
        def lookup(self,*args):
            result=super().lookup(*args)
            if args[2].site=='MY' and result.actual_price is not None:
                return replace(result,actual_price=Decimal('12.65'))
            return result
    enable(factory)
    service = PricingService(factory, BelowMargin(cfg.runtime_dir), cfg)
    service.run_batch('one-percent-readback')
    service.reconcile()
    with factory() as session:
        records=[e for e in session.query(Execution).all() if e.request['key']['site']=='MY']
        assert records
        assert all(e.state=='mismatch' for e in records)
    assert 'MY' not in settings_value(factory)['paused_sites']

