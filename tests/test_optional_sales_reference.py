import pytest
from test_pricing import single

@pytest.mark.parametrize("fields", [
    {"sales_7d": 21}, {"sales_7d": 999999},
    {"sales_7d": None, "sales_complete": False, "sales_basis": None, "sales_observed_at": None},
])
def test_sales_reference_never_protects_or_blocks(fields):
    assert single(online_fields=fields).ready
