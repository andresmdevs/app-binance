from decimal import Decimal

from binance.risk import RiskLimits, evaluate_open

L = RiskLimits(max_order_notional=Decimal("1000"), max_position_notional=Decimal("5000"),
               max_open_positions=2, max_leverage=10, daily_loss_limit=Decimal("100"))
BASE = dict(symbol="BTCUSDT", open_symbols=set(),
            symbol_notional=Decimal("0"), daily_realized=Decimal("0"))


def test_ok():
    assert evaluate_open(L, order_notional=Decimal("100"), **BASE) is None


def test_order_too_big():
    assert evaluate_open(L, order_notional=Decimal("2000"), **BASE)


def test_kill_switch():
    assert evaluate_open(L, order_notional=Decimal("10"),
                         **{**BASE, "daily_realized": Decimal("-100")})


def test_max_positions():
    assert evaluate_open(L, order_notional=Decimal("10"),
                         **{**BASE, "symbol": "X", "open_symbols": {"A", "B"}})


def test_existing_symbol_not_blocked_when_full():
    assert evaluate_open(L, order_notional=Decimal("10"),
                         **{**BASE, "symbol": "A", "open_symbols": {"A", "B"}}) is None


def test_exposure_cap():
    assert evaluate_open(L, order_notional=Decimal("100"),
                         **{**BASE, "symbol_notional": Decimal("4950")})
