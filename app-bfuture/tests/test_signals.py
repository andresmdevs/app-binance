from binance.signals import (
    Gates,
    cluster_liquidations,
    composite_score,
    depth_imbalance,
    directional_bias,
    estimated_liq_levels,
    high_volume_nodes,
    latest_taker_ratio,
    net_rr,
    oi_change_pct,
    passes_gates,
    profile_fit,
    rsi,
    safe_sizing,
    sma,
    sr_zones,
    swing_levels,
    trend_direction,
    volume_profile,
)


def kline(high, low, close=0.0, qvol=1.0):
    # [openTime, open, high, low, close, volume, closeTime, quoteVolume, ...]
    return [0, 0.0, high, low, close, 0.0, 1, qvol, 0, 0.0, 0.0, 0]


# Mercado concentrado en ~100, con colas finas en 91 y 109.
KL = ([kline(101, 99, 100, 1000)] * 5
      + [kline(92, 90, 91, 40), kline(110, 108, 109, 40)])


def test_volume_profile_finds_hvn():
    prof = volume_profile(KL, bins=20)
    top = high_volume_nodes(prof, top_k=1)[0]
    assert 99 <= top.center <= 101  # el nodo de alto volumen cae en ~100


def test_sr_zones_split_by_price():
    prof = volume_profile(KL, bins=20)
    zones = sr_zones(prof, current_price=105, top_k=6)
    assert any(z.kind == "support" and z.mid < 105 for z in zones)
    assert any(z.kind == "resistance" and z.mid > 105 for z in zones)
    # la más cercana a 105 va primero
    assert abs(zones[0].mid - 105) <= abs(zones[-1].mid - 105)


def test_swing_levels():
    kl = [kline(1, 5), kline(2, 4), kline(3, 3), kline(2, 4), kline(1, 5)]
    sh, sl = swing_levels(kl, lookback=1)
    assert sh == [3] and sl == [3]


def test_cluster_liquidations():
    prints = [(100.0, 10, "long"), (100.2, 5, "long"), (120.0, 8, "short")]
    cl = cluster_liquidations(prints, bin_pct=0.005)
    assert cl[0].qty == 15 and cl[0].side == "long"          # cluster mayor primero
    assert any(abs(c.mid - 120) < 1 and c.side == "short" for c in cl)


def test_estimated_liq_levels():
    assert estimated_liq_levels(100, "long", (100,)) == [99.0]
    assert estimated_liq_levels(100, "short", (100,)) == [101.0]


def test_oi_and_taker_helpers():
    assert oi_change_pct([{"sumOpenInterest": "100"}, {"sumOpenInterest": "110"}]) == 10.0
    assert oi_change_pct([{"sumOpenInterest": "100"}]) is None
    assert latest_taker_ratio([{"buySellRatio": "1.2"}]) == 1.2
    assert latest_taker_ratio([]) is None


def test_directional_bias():
    assert directional_bias(1.0, 5.0, 0.0, 1.2).label == "long"
    assert directional_bias(-1.0, 5.0, 0.001, 0.8).label == "short"
    assert directional_bias(0.0, None, None, None).label == "neutral"


def test_profile_fit():
    assert profile_fit(True, 55, 55) == 1.0
    assert profile_fit(False, None, None) == 0.0


def test_net_rr_penaliza_fees():
    assert net_rr(100, 98, 104, fee_roundtrip=0.0) == 2.0
    assert net_rr(100, 98, 104, fee_roundtrip=0.001) < 2.0  # fees bajan el R:R


def test_gates():
    g = Gates()
    ok, _ = passes_gates(2.0, 2_000_000, 5, 35, g)
    assert ok
    bad, reasons = passes_gates(1.0, 2_000_000, 5, 35, g)
    assert not bad and reasons
    bad2, reasons2 = passes_gates(2.0, 2_000_000, 50, 35, g)  # no cabe en capacidad
    assert not bad2 and any("capacidad" in r for r in reasons2)


def test_composite_score():
    assert composite_score({k: 1.0 for k in
                            ("structure", "flow", "liquidity_fuel",
                             "profile_fit", "rr_quality")}) == 100.0
    assert composite_score({"structure": 1.0}) == 30.0


def test_sma_rsi_trend():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1], 2) is None
    assert rsi(list(range(1, 20))) == 100.0            # solo subidas -> 100
    assert rsi([1, 1, 1]) is None                      # datos insuficientes
    assert trend_direction([1] * 20 + list(range(1, 40))) == "up"


def test_depth_imbalance():
    d = {"bids": [["100", "3"]], "asks": [["101", "1"]]}
    assert depth_imbalance(d) > 0                       # más compra -> soporte
    assert depth_imbalance({"bids": [], "asks": []}) == 0.0


def test_safe_sizing_normal():
    # 6.95 saldo, SL 3%, mínimo notional 5, tope 10x
    z = safe_sizing(6.95, 0.03, 5.0, risk_frac=0.10, max_leverage=10)
    assert z["leverage"] == 10          # lev seguro (20) capado a 10
    assert not z["sl_wide"]
    assert abs(z["margin"] - 0.695) < 1e-6
    assert abs(z["notional"] - 6.95) < 1e-6


def test_safe_sizing_wide_sl_flags():
    # SL 10% -> lev seguro ~6 no llega al mínimo notional (necesita 8) -> avisa
    z = safe_sizing(6.95, 0.10, 5.0, risk_frac=0.10, max_leverage=10)
    assert z["sl_wide"] and z["leverage"] == 8


def test_safe_sizing_respects_cap_and_raises_with_it():
    # SL apretado 2%: con tope 10x usa 10; subiendo el tope a 25x aprovecha más
    assert safe_sizing(6.95, 0.02, 5.0, max_leverage=10)["leverage"] == 10
    assert safe_sizing(6.95, 0.02, 5.0, max_leverage=25)["leverage"] == 25
