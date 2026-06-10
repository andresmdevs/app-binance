from binance.analysis import (ema_series, reconstruct_trades, rsi_last,
                              summarize_trades)

TRADES = [
    {"symbol": "BTCUSDT", "side": "BUY", "qty": "0.001", "price": "60000", "commission": "0.012", "realizedPnl": "0", "time": 1000, "id": 1},
    {"symbol": "BTCUSDT", "side": "SELL", "qty": "0.001", "price": "60100", "commission": "0.012", "realizedPnl": "0.1", "time": 61000, "id": 2},
    {"symbol": "BTCUSDT", "side": "BUY", "qty": "0.001", "price": "60100", "commission": "0.012", "realizedPnl": "0", "time": 120000, "id": 3},
    {"symbol": "BTCUSDT", "side": "SELL", "qty": "0.001", "price": "59900", "commission": "0.012", "realizedPnl": "-0.2", "time": 180000, "id": 4},
]


def test_reconstruct_winner_and_loser():
    trips = reconstruct_trades(TRADES, [{"time": 1500, "income": "-0.005"}])
    assert len(trips) == 2
    win, lose = trips
    assert win.direction == "LONG"
    assert abs(win.net_pnl - (0.1 - 0.024 - 0.005)) < 1e-9
    assert abs(win.duration_s - 60) < 1e-9
    assert abs(win.funding - (-0.005)) < 1e-9
    assert lose.net_pnl < 0 and abs(lose.funding) < 1e-12


def test_reconstruct_short():
    trades = [
        {"symbol": "X", "side": "SELL", "qty": "1", "price": "100", "commission": "0", "realizedPnl": "0", "time": 1, "id": 1},
        {"symbol": "X", "side": "BUY", "qty": "1", "price": "90", "commission": "0", "realizedPnl": "10", "time": 2, "id": 2},
    ]
    t = reconstruct_trades(trades)[0]
    assert t.direction == "SHORT" and t.entry == 100 and t.exit == 90


def test_summarize_segments():
    s = summarize_trades(reconstruct_trades(TRADES))
    assert (s["count"], s["wins"], s["losses"]) == (2, 1, 1)
    assert s["winners"]["count"] == 1 and s["losers"]["count"] == 1


def test_ema_rsi():
    assert len(ema_series([1, 2, 3, 4], 2)) == 4
    assert rsi_last(list(range(1, 16)), 14) == 100.0  # solo subidas
    assert rsi_last([1, 2], 14) is None
