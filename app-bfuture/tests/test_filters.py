from decimal import ROUND_DOWN, Decimal

import pytest

from binance.filters import FilterError, _decimals_of, _round_to_step


def test_round_step():
    assert _round_to_step(Decimal("1.27"), Decimal("0.1"), ROUND_DOWN) == Decimal("1.2")


def test_decimals_of():
    assert _decimals_of(Decimal("0.001")) == 3
    assert _decimals_of(Decimal("1")) == 0
    assert _decimals_of(Decimal("0.10")) == 1


def test_format(sf):
    assert sf.format_price(64000.07) == "64000.1"
    assert sf.format_qty(0.0019) == "0.001"  # floor a step


def test_validate_ok(sf):
    assert sf.validate(side="BUY", quantity=0.002, price=60000, mark_price=60000) == {
        "quantity": "0.002", "price": "60000.0"}


def test_validate_market(sf):
    adj = sf.validate(side="SELL", quantity=0.0021, order_type="MARKET", mark_price=60000)
    assert adj["price"] is None and adj["quantity"] == "0.002"


def test_validate_min_notional(sf):
    with pytest.raises(FilterError):
        sf.validate(side="BUY", quantity=0.001, price=0.10)  # notional ~0.0001 < 5


def test_validate_qty_rounds_to_zero(sf):
    with pytest.raises(FilterError):
        sf.validate(side="BUY", quantity=0.0004, price=60000)  # < step -> 0


def test_validate_percent_price_band(sf):
    with pytest.raises(FilterError):
        sf.validate(side="BUY", quantity=0.002, price=70000, mark_price=60000)
