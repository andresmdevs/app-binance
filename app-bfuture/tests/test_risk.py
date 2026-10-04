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


# --- RiskManager: el kill-switch se distingue del resto de bloqueos ----------
import asyncio

import pytest

from binance.risk import KillSwitchError, RiskError, RiskManager


class _Acct:
    def __init__(self, daily):
        self.daily = daily

    async def position_risk(self, symbol=None):
        return []

    async def income(self, **kw):
        return [{"income": str(self.daily)}]


def test_check_open_raises_kill_switch_subclass():
    rm = RiskManager(_Acct(-150), L)
    with pytest.raises(KillSwitchError):
        asyncio.run(rm.check_open("BTCUSDT", Decimal("10")))


def test_check_open_other_block_is_plain_risk_error():
    rm = RiskManager(_Acct(0), L)
    with pytest.raises(RiskError) as ei:
        asyncio.run(rm.check_open("BTCUSDT", Decimal("5000")))
    assert not isinstance(ei.value, KillSwitchError)


def test_limits_from_env(monkeypatch):
    from binance.risk import limits_from_env
    monkeypatch.setenv("RISK_MAX_OPEN_POSITIONS", "3")
    monkeypatch.setenv("RISK_DAILY_LOSS_LIMIT", "25")
    lim = limits_from_env()
    assert lim.max_open_positions == 3 and lim.daily_loss_limit == Decimal("25")
