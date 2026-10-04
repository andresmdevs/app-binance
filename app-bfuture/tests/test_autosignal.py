import math

import pytest

from binance.autosignal import (ShadowBoard, SignalConfig, atr,
                                bollinger, decide, indicator_votes, macd_hist,
                                max_losing_streak, simulate, summarize_sim,
                                taker_buy_ratio, wilson_interval)


def bar(i, o, c, rng=0.002, buy=0.5, vol=100.0, h=None, lo=None):
    h = h if h is not None else max(o, c) * (1 + rng)
    lo = lo if lo is not None else min(o, c) * (1 - rng)
    return [i * 60_000, o, h, lo, c, vol, i * 60_000 + 59_999, vol * c, 1,
            vol * buy, vol * buy * c, 0]


def series(closes, rng=0.002, buy=0.5):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(bar(i, prev, c, rng, buy))
        prev = c
    return out


def crash():
    """Lateral y luego caída fuerte con venta agresiva: los de tendencia dicen
    SHORT y los de rebote (RSI/Bollinger) dicen LONG -> justo el caso Exnova."""
    closes = [100 + 0.05 * math.sin(i) for i in range(93)] + [99, 98, 97, 96, 95, 94]
    return series(closes, buy=0.3)


# --- Indicadores -------------------------------------------------------------
def test_macd_hist_positive_when_accelerating_up():
    closes = [100 * (1.0005 ** (i * i / 50)) for i in range(80)]
    assert macd_hist(closes) > 0
    assert macd_hist(closes[:20]) is None            # sin datos suficientes


def test_bollinger_flat_series_collapses():
    lo, mid, hi = bollinger([10.0] * 25)
    assert lo == mid == hi == 10.0


def test_atr_constant_range():
    highs, lows, closes = [101.0] * 30, [99.0] * 30, [100.0] * 30
    assert atr(highs, lows, closes, 14) == pytest.approx(2.0)


def test_taker_buy_ratio():
    kl = [bar(i, 100, 100, buy=0.7) for i in range(5)]
    assert taker_buy_ratio(kl, 5) == pytest.approx(0.7)
    assert taker_buy_ratio([[0, 1, 1, 1, 1, 0, 0]], 5) is None   # sin columnas taker


def test_votes_crash_split_trend_vs_rebound():
    votes, values = indicator_votes(crash())
    assert votes["trend"] == -1 and votes["macd"] == -1 and votes["flow"] == -1
    assert votes["rsi"] == 1 and votes["bollinger"] == 1     # sobreventa: esperan rebote
    assert values["rsi"] < 30


# --- Decisión ------------------------------------------------------------------
def test_decide_skips_weak_signal_instead_of_tiebreak():
    d = decide(crash())
    assert d.side is None and d.score == -1
    assert "señal débil" in d.reason


def test_decide_with_trend_votes_only_goes_short():
    cfg = SignalConfig(enabled=("trend", "macd", "flow"))
    d = decide(crash(), cfg)
    assert d.side == "SELL" and d.score == -3
    assert d.tp_pct == pytest.approx(1.5 * d.atr_pct)
    assert d.sl_pct == pytest.approx(1.0 * d.atr_pct)
    assert "Tendencia" in d.reason


def test_decide_min_score_threshold():
    assert decide(crash(), SignalConfig(min_score=1)).side == "SELL"


def test_decide_low_volatility_gate():
    closes = [100 + 0.01 * math.sin(i) for i in range(99)]
    d = decide(series(closes, rng=0.0001), SignalConfig(min_score=0))
    assert d.side is None and "poca volatilidad" in d.reason


def test_decide_insufficient_data():
    d = decide(crash()[:30])
    assert d.side is None and "insuficientes" in d.reason


# --- Marcador en sombra -----------------------------------------------------------
def test_wilson_interval():
    lo, hi = wilson_interval(50, 100)
    assert lo == pytest.approx(0.404, abs=0.002) and hi == pytest.approx(0.596, abs=0.002)
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_shadow_board_scores_each_vote_and_decision():
    sb = ShadowBoard()
    sb.record("X", 1000, {"trend": 1, "rsi": -1, "macd": 0}, "BUY")
    assert sb.resolve("X", 1000, 100.0, 101.0)           # vela verde
    t = sb.tallies
    assert (t["trend"].hits, t["trend"].total) == (1, 1)
    assert (t["rsi"].hits, t["rsi"].total) == (0, 1)
    assert t["macd"].total == 0                           # 0 = sin opinión, no cuenta
    assert (t["decision"].hits, t["decision"].total) == (1, 1)
    assert not sb.resolve("X", 1000, 100.0, 99.0)         # ya resuelta
    sb.record("X", 2000, {"trend": 1}, None)
    sb.resolve("X", 2000, 100.0, 100.0)                   # vela plana: no cuenta
    assert t["trend"].total == 1


def test_shadow_resolve_klines_only_closed():
    sb = ShadowBoard()
    kl = [bar(0, 100, 101), bar(1, 101, 100)]
    sb.record("X", 0, {"trend": 1}, "BUY")
    sb.record("X", 60_000, {"trend": 1}, "BUY")
    sb.resolve_klines("X", kl, now_ms=60_000)             # la 2ª sigue abierta
    assert sb.tallies["trend"].total == 1 and (("X", 60_000) in sb._pending)
    ranked = sb.ranking()
    assert ranked[0]["total"] > 0


# --- Replay ----------------------------------------------------------------------
def test_simulate_tp_sl_and_time_exits(monkeypatch):
    from binance import autosignal as a
    from binance.autosignal import Decision

    def fake_decide(kl, cfg=None):  # SHORT en cada vela con TP 1% / SL 1%
        return Decision("SELL", -3, {"trend": -1}, {}, float(kl[-1][4]), 0.01, 0.01, 0.01, "t")
    monkeypatch.setattr(a, "decide", fake_decide)
    base = [bar(i, 100, 100, rng=0.0) for i in range(60)]
    kl = base + [
        bar(60, 100, 100, h=100.2, lo=98.5),   # entra short en 100 -> toca TP (99)
        bar(61, 100, 100, h=101.5, lo=98.5),   # toca SL y TP en la misma vela -> SL
        bar(62, 100, 100.5, h=100.6, lo=99.8), # ni TP ni SL -> sale por tiempo
        bar(63, 100, 100, rng=0.0),
    ]
    trades = simulate(kl, SignalConfig(fee_rt=0.001), hold_candles=1, slippage_bps=0)
    assert [t.reason for t in trades[:3]] == ["tp", "sl", "tiempo"]
    assert trades[0].net_pct == pytest.approx(0.01 - 0.001)
    assert trades[1].net_pct == pytest.approx(-0.01 - 0.001)
    assert trades[2].net_pct == pytest.approx(-0.005 - 0.001)


def test_simulate_one_position_at_a_time(monkeypatch):
    from binance import autosignal as a
    from binance.autosignal import Decision
    monkeypatch.setattr(a, "decide", lambda kl, cfg=None: Decision(
        "BUY", 3, {}, {}, 100.0, 0.5, 0.5, 0.5, "t"))      # TP/SL lejanos
    kl = [bar(i, 100, 100, rng=0.0) for i in range(70)]
    trades = simulate(kl, SignalConfig(), hold_candles=3, slippage_bps=0)
    opens = [t.open_time // 60_000 for t in trades]
    assert opens == sorted(opens) and all(b - a >= 3 for a, b in zip(opens, opens[1:]))


def test_simulate_random_walk_loses_the_fees():
    import random
    rnd = random.Random(7)
    closes = [100.0]
    for _ in range(2500):
        closes.append(closes[-1] * (1 + rnd.gauss(0, 0.003)))
    kl = series(closes, rng=0.002, buy=0.5)
    sb = ShadowBoard()
    s = summarize_sim(simulate(kl, shadow=sb), margin=5, leverage=5)
    assert s["trades"] > 100
    assert s["avg_net_pct"] < 0               # sin ventaja, las comisiones mandan
    assert sb.tallies["decision"].total > 100


def test_summary_and_streak():
    assert max_losing_streak([0.1, -0.1, -0.2, 0.0, 0.3, -0.1]) == 3
    assert summarize_sim([]) == {"trades": 0}
