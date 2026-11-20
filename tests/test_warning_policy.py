from test_pricing import single, contract
import pytest

@pytest.mark.parametrize("fields,code", [({"history_known":False},"unknown_history"), ({"sellable":False},"not_sellable")])
def test_warning_only_conditions_continue_calculation(fields,code):
    result=single(online_fields=fields)
    assert result.ready
    assert code in result.trace["warning_codes"]
    assert result.trace["warnings"]

@pytest.mark.parametrize("fields,snap,code", [({}, {"contracts":{}}, "unverified_site_contract"), ({"profit_check":None}, {}, "unverified_online_profit"), ({}, {"contracts":{"MY":contract(warehouses=[])}}, "unsupported_warehouse")])
def test_unusable_calculation_basis_is_excluded_and_warned(fields,snap,code):
    result=single(online_fields=fields,snapshot_fields=snap)
    assert result.status=="本轮不调"
    assert not result.ready and result.upload_price is None
    assert code in result.trace["warning_codes"]
