"""Orquestación del motor de señales (async, con red).

Baja datos reales de Binance (klines, OI, taker ratio, funding, libro), llama a
las funciones puras de `signals` y arma `Setup`s ordenados. Lo usan tanto el
dry-run de terminal como la pestaña Radar de la UI.

Dry-run (solo lectura, mainnet, SIN claves — todos estos endpoints son públicos):

    PYTHONPATH=src .venv/bin/python -m binance.scanner [n_simbolos]
"""
from __future__ import annotations

import asyncio
from typing import Optional

from . import market as market_mod
from . import signals as sg
from .client import BinanceFuturesClient
from .filters import FilterCache
from .signals import Gates, Setup

STOP_BUFFER = 0.003          # 0.3% más allá de la zona = invalidación estructural
MAKER_FEE_RT = 0.0006        # ~0.06% ida+vuelta si entras maker + cierras
NEAR_PCT = 0.02              # el precio debe estar a <=2% de la zona de entrada
MAX_TARGET_PCT = 0.06        # objetivo realista: no más lejos del 6% (mata R:R fantasía)
ENTRY_NEAR_GATE = 0.03       # el precio debe estar a <=3% de la zona para ser accionable ya


async def _safe(coro, default=None):
    try:
        return await coro
    except Exception:
        return default


def _reason(side: str, bias: sg.Bias, aligned: bool, rr: float) -> str:
    kind = "soporte" if side == "long" else "resistencia"
    parts = [f"Rechazo/rebote en {kind} de volumen"]
    parts += bias.notes[:2]
    if aligned:
        parts.append("alineado con la tendencia (como tus ganadoras)")
    parts.append(f"R:R neto {rr:.1f}")
    return " · ".join(parts)


async def scan_symbol(client, symbol: str, *, capacity: float = 70.0,
                      min_notional: float = 5.0, winner_stats: Optional[dict] = None,
                      gates: Optional[Gates] = None, interval: str = "15m",
                      limit: int = 200) -> Optional[Setup]:
    """Devuelve un `Setup` para el símbolo, o None si no hay oportunidad clara."""
    gates = gates or Gates()
    kl = await _safe(client.klines(symbol, interval, limit=limit), [])
    if not kl or len(kl) < 60:
        return None
    closes = [float(k[4]) for k in kl]
    price = closes[-1]

    tk = await _safe(client.ticker_24h(symbol), {}) or {}
    change = float(tk.get("priceChangePercent", 0) or 0)
    qvol = float(tk.get("quoteVolume", 0) or 0)

    # --- Flujo / sesgo ---
    oi_hist = await _safe(client.open_interest_hist(symbol, period="5m", limit=12), [])
    taker = await _safe(client.taker_long_short_ratio(symbol, period="5m", limit=6), [])
    mp = await _safe(client.mark_price(symbol), {}) or {}
    if isinstance(mp, list):
        mp = mp[0] if mp else {}
    fr = mp.get("lastFundingRate")
    funding = float(fr) if fr not in (None, "") else None
    bias = sg.directional_bias(change, sg.oi_change_pct(oi_hist), funding,
                               sg.latest_taker_ratio(taker))
    if bias.label == "neutral":
        return None
    side = bias.label

    # --- Estructura: zonas + entrada/stop/objetivo ---
    zones = sg.sr_zones(sg.volume_profile(kl, bins=48), price, top_k=8)
    supports = sorted((z for z in zones if z.kind == "support"),
                      key=lambda z: abs(z.mid - price))
    resistances = sorted((z for z in zones if z.kind == "resistance"),
                         key=lambda z: abs(z.mid - price))
    if side == "long":
        entry_zone = supports[0] if supports else None
        target_zone = min((r for r in resistances), key=lambda z: z.mid, default=None)
        if not entry_zone or not target_zone:
            return None
        entry_ref, stop, target = entry_zone.high, entry_zone.low * (1 - STOP_BUFFER), target_zone.mid
        if target <= entry_ref:
            return None
    else:
        entry_zone = resistances[0] if resistances else None
        target_zone = max((s for s in supports), key=lambda z: z.mid, default=None)
        if not entry_zone or not target_zone:
            return None
        entry_ref, stop, target = entry_zone.low, entry_zone.high * (1 + STOP_BUFFER), target_zone.mid
        if target >= entry_ref:
            return None

    # Solo setups ACCIONABLES ya: el precio debe estar en la zona de entrada, para
    # que una entrada a mercado ≈ la entrada estructural (evita entrar 6% arriba/abajo).
    if abs(price - entry_zone.mid) / price > ENTRY_NEAR_GATE:
        return None

    # Tope de cordura: un objetivo a +18% con stop pegado da un R:R fantasía que
    # el precio no alcanza en el horizonte del scalp. Lo acotamos a algo realista.
    if side == "long":
        target = min(target, entry_ref * (1 + MAX_TARGET_PCT))
    else:
        target = max(target, entry_ref * (1 - MAX_TARGET_PCT))

    rr = sg.net_rr(entry_ref, stop, target, fee_roundtrip=MAKER_FEE_RT)

    # --- Componentes del score ---
    dist = abs(price - entry_zone.mid) / price if price else 1.0
    proximity = max(0.0, 1.0 - dist / NEAR_PCT)
    depth = await _safe(client.depth(symbol, limit=100), {}) or {}
    imb = sg.depth_imbalance(depth, levels=50)
    trend = sg.trend_direction(closes)
    aligned = (side == "long" and trend == "up") or (side == "short" and trend == "down")
    entry_rsi = sg.rsi(closes)
    components = {
        "structure": min(1.0, entry_zone.strength * 0.5 + proximity * 0.5),
        "flow": abs(bias.score),
        "liquidity_fuel": max(0.0, imb if side == "long" else -imb),
        "profile_fit": sg.profile_fit(aligned, entry_rsi,
                                      (winner_stats or {}).get("avg_entry_rsi")),
        "rr_quality": min(1.0, max(0.0, (rr - 1.0) / 2.0)),
    }
    ok, _ = sg.passes_gates(rr, qvol, min_notional, capacity, gates)
    if not ok:
        return None
    return Setup(
        symbol=symbol, side=side, entry_low=entry_zone.low, entry_high=entry_zone.high,
        stop=stop, target=target, rr_net=round(rr, 2),
        score=sg.composite_score(components),
        components={k: round(v, 2) for k, v in components.items()},
        reason=_reason(side, bias, aligned, rr),
    )


async def scan_market(client, symbols: list[str], *, concurrency: int = 6,
                      top_n: int = 3, **kw) -> list[Setup]:
    """Escanea `symbols` en paralelo y devuelve los mejores `top_n` setups."""
    sem = asyncio.Semaphore(concurrency)

    async def one(s):
        async with sem:
            try:
                return await scan_symbol(client, s, **kw)
            except Exception:
                return None

    results = await asyncio.gather(*[one(s) for s in symbols])
    return sg.rank_setups([r for r in results if r], top_n=top_n)


# --- Dry-run de terminal (mainnet, solo lectura, sin claves) -----------------
async def _cli(n: int) -> None:
    cli = BinanceFuturesClient(testnet=False)  # mainnet público
    async with cli:
        await cli.sync_time()
        filters = FilterCache(cli)
        await filters.refresh()
        tickers = await market_mod.fetch_tickers(cli, allowed=filters.symbols())
        liquid = sorted(tickers, key=lambda t: t.quote_volume, reverse=True)[:n]
        symbols = [t.symbol for t in liquid]
        print(f"Escaneando {len(symbols)} símbolos más líquidos (mainnet, solo lectura)…",
              flush=True)
        setups = await scan_market(cli, symbols, top_n=5, capacity=70, min_notional=5)
        print("=" * 70)
        if not setups:
            print("Sin setups que pasen los gates ahora mismo (mercado sin estructura clara).")
            return
        for s in setups:
            print(f"\n[{s.score:>5}] {s.symbol:<12} {s.side.upper()}")
            print(f"   entrada {s.entry_low:.6g}–{s.entry_high:.6g} · stop {s.stop:.6g} · "
                  f"objetivo {s.target:.6g} · R:R {s.rr_net}")
            print(f"   componentes {s.components}")
            print(f"   {s.reason}")


if __name__ == "__main__":
    import sys
    asyncio.run(_cli(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
