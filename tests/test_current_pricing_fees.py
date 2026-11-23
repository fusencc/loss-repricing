import pytest


def test_th_public_category_vat_is_not_used_as_a_default():
    from app.adapters.current_pricing_fees import public_values
    rows = [{'platform': 'TIKTOK', 'site': 'TH', 'rateType': t,
             'rateFormat': 1 if t == 4 else 0, 'currency': 'RMB' if t == 4 else '',
             'associationCategory': t == 0, 'expense': '1'} for t in (0, 1, 2, 4)]
    assert public_values(rows, 'TH')['vatRate'] is None


def test_live_selector_rejects_old_fee_archive_and_does_not_fall_back():
    from app.adapters.current_pricing_fees import select_current_rate
    with pytest.raises(ValueError, match='当天'):
        select_current_rate({'schema_version': 'user-fixed-pricing-fees-v2'}, {}, {}, 'PH', 'direct')


def test_public_rate_missing_or_ambiguous_does_not_reuse_fixed_value():
    from app.adapters.current_pricing_fees import public_values
    with pytest.raises(ValueError):
        public_values([], 'PH')
    rows = [{'platform': 'TIKTOK', 'site': 'PH', 'rateType': t, 'rateFormat': 0,
             'associationCategory': False, 'expense': '1'} for t in (0, 1, 2, 2, 4)]
    with pytest.raises(ValueError):
        public_values(rows, 'PH')
