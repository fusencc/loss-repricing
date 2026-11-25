"""日志毛利投影按选中订单去重，不能按来源次数重复或抹掉原金额。"""
from types import SimpleNamespace
import pytest
from app.services.records import public_record


def view(rows, selected='selected'):
    key = dict(platform='TikTok', shop_id='shop', site='MY', product_id='product',
               item_id='variant', sku='SKU', warehouse='warehouse')
    item = SimpleNamespace(id='item', batch_id='batch', key=key, status='待处理', phase='blocked',
        reason='原因', updated_at='2026-11-25T01:00:00+00:00', decision={
            'selected_row_id': selected, 'unit_loss': '999.99', 'trace': {'source_rows': rows}})
    batch = SimpleNamespace(id='batch', business_date='2026-11-25', mode='live', snapshot={})
    return public_record(item, batch, None, 'Tiktok一部', {'id': 'seller', 'name': '销售员'})


def row(**changes):
    return dict({'row_id': 'selected', 'order_id': 'order-1', 'basis': 'order',
        'fee_scope': 'erp-corrected-first-margin-v1', 'loss_amount': '-0.2979', 'currency': 'CNY',
        'source': 'loss_pending'}, **changes)


def test_same_selected_order_in_two_sources_keeps_one_original_margin():
    data = view([row(), row(source='blocked_pending')])
    assert data['first_margin'] == {'amount': '-0.2979', 'currency': 'CNY'}
    assert data['corrected_first_margin'] == data['first_margin']


def test_equal_decimal_values_preserve_source_precision_and_do_not_sum():
    data = view([row(loss_amount='-12.3000'), row(loss_amount='-12.30', source='blocked_pending')])
    assert data['first_margin'] == {'amount': '-12.3000', 'currency': 'CNY'}


@pytest.mark.parametrize('changes', [
    {'loss_amount': '-0.5'}, {'currency': 'MYR'}, {'order_id': 'other-order'},
    {'basis': 'unit'}, {'fee_scope': 'unknown'}, {'loss_amount': 'NaN'}, {'currency': None},
])
def test_conflicting_or_invalid_order_evidence_stays_missing(changes):
    assert view([row(), row(**changes)])['first_margin'] is None


def test_only_selected_order_is_displayed_without_other_orders_or_unit_loss_substitution():
    assert view([row(row_id='another', loss_amount='-888')])['first_margin'] is None
    assert view([row()], selected=None)['first_margin'] is None
    assert view([row(loss_amount='0')])['first_margin'] == {'amount': '0', 'currency': 'CNY'}
