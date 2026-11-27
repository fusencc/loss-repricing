from app.domain.engine import evaluate
from app.services.erp_pricing import apply_recent_price_protection
from test_pricing import key,line,snapshot,NOW


def test_protected_missing_online_is_no_adjustment_and_unknown_stays_pending():
    a=key();b=key(item_id='other',sku='B')
    decisions=evaluate(snapshot([line(a),line(b,row_id='r2',order_id='o2')],[]),NOW)
    assert all(d.reason_code=='missing_mapping' for d in decisions)
    apply_recent_price_protection(decisions,{'source_id':'official-test','records':{
        a.item_id:{'status':'ok','count':1},b.item_id:{'status':'ok','count':0}}})
    assert decisions[0].status=='不调价' and decisions[0].reason_code=='recent_price_change'
    assert decisions[0].reason == '近三天存在1条调价日志'
    assert not decisions[0].ready
    assert decisions[1].status=='待处理' and decisions[1].reason_code=='missing_mapping'
