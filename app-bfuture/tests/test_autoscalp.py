import asyncio
from decimal import Decimal

from binance import scalp
from binance.autoscalp import (auto_levels, auto_notional, momentum_ok,
                               next_stop_level)
from binance.forwardtest import select_candidates
from binance.market import parse_tickers
from binance.trade import FuturesTrader


def run(coro):
    return asyncio.run(coro)


def test_auto_levels():
    tp, sl = auto_levels(10, target_roe=0.35, stop_roe=0.5)
    assert abs(tp - 0.035) < 1e-9          # 35% ROE / 10x = 3.5% precio
    assert abs(sl - 0.05) < 1e-9
    tp2, sl2 = auto_levels(50, target_roe=0.35, stop_roe=0.5)
    assert abs(tp2 - 0.007) < 1e-9
    assert sl2 <= 0.85 / 50 + 1e-9          # acotado por margen anti-liquidación


def test_auto_notional():
    assert auto_notional("0.30", 75) == Decimal("22.50")


def test_momentum_ok_long():
    ok, _ = momentum_ok(side="BUY", last_price=0.25, change_pct_24h=30,
                        recent_return_pct=1.2, max_price=0.5)
    assert ok
    bad_price, _ = momentum_ok(side="BUY", last_price=5, change_pct_24h=30,
                               recent_return_pct=1, max_price=0.5)
    assert not bad_price
    weak24, _ = momentum_ok(side="BUY", last_price=0.2, change_pct_24h=1,
                            recent_return_pct=1, max_price=0.5)
    assert not weak24
    weak1m, _ = momentum_ok(side="BUY", last_price=0.2, change_pct_24h=30,
                            recent_return_pct=-0.5, max_price=0.5)
    assert not weak1m


def test_momentum_ok_short():
    ok, _ = momentum_ok(side="SELL", last_price=0.25, change_pct_24h=-30,
                        recent_return_pct=-1.2, max_price=0.5)
    assert ok
    not_bear, _ = momentum_ok(side="SELL", last_price=0.25, change_pct_24h=30,
                              recent_return_pct=-1, max_price=0.5)
    assert not not_bear


def test_next_stop_level_long_trails_up_only():
    # por debajo del disparador -> no mueve
    assert next_stop_level("BUY", 100, 100.4, None, be_trigger_pct=0.01, trail_pct=0.005) is None
    # supera disparador -> sube a BE/trailing
    s1 = next_stop_level("BUY", 100, 101.5, None, be_trigger_pct=0.01, trail_pct=0.005)
    assert s1 and s1 > 100
    # con stop ya alto, un precio menor NO lo baja
    assert next_stop_level("BUY", 100, 101.0, s1, be_trigger_pct=0.01, trail_pct=0.005) is None
    # precio mayor -> trailing sube
    s2 = next_stop_level("BUY", 100, 103.0, s1, be_trigger_pct=0.01, trail_pct=0.005)
    assert s2 and s2 > s1


def test_next_stop_level_short():
    s = next_stop_level("SELL", 100, 98.5, None, be_trigger_pct=0.01, trail_pct=0.005)
    assert s and s < 100


def test_select_candidates_short_direction():
    rows = [
        {"symbol": "UPUSDT", "lastPrice": "0.2", "priceChangePercent": "20", "quoteVolume": "2e6"},
        {"symbol": "DNUSDT", "lastPrice": "0.3", "priceChangePercent": "-15", "quoteVolume": "2e6"},
        {"symbol": "DN2USDT", "lastPrice": "0.1", "priceChangePercent": "-30", "quoteVolume": "2e6"},
    ]
    t = parse_tickers(rows)
    valid = {"UPUSDT", "DNUSDT", "DN2USDT"}
    short = select_candidates(t, valid, max_price=0.5, min_quote_volume=1e6, top_n=5, direction="SELL")
    assert short == ["DN2USDT", "DNUSDT"]   # más bajistas primero; el alcista excluido


class _TrailFake:
    """Cliente falso con mark price creciente y posición que cierra tras N polls."""
    def __init__(self):
        self.calls = []
        self.n = 0
        self.ws_base = "x"

    async def position_risk(self, symbol=None):
        self.n += 1
        if self.n > 4:
            return []
        return [{"symbol": symbol, "positionAmt": "1", "entryPrice": "100"}]

    async def mark_price(self, symbol=None):
        return {"markPrice": str(100 + self.n * 0.6)}  # sube cada poll

    async def _request(self, method, path, params=None, *, signed=False):
        self.calls.append((method, path))
        return {"algoId": 999}


def test_manage_trailing_stop_moves_stop(filters):
    c = _TrailFake()
    t = FuturesTrader(c, filters)
    run(scalp.manage_trailing_stop(
        t, c, symbol="BTCUSDT", side="BUY", entry=100.0, initial_stop=98.0,
        sl_algo_id=1, be_trigger_pct=0.005, trail_pct=0.005,
        max_hold_seconds=100, poll_seconds=0))
    placed = [x for x in c.calls if x == ("POST", "/fapi/v1/algoOrder")]
    assert placed  # colocó al menos un nuevo STOP (trailing)
