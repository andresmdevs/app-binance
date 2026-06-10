"""Resumen de mercado general y Top movers (USDⓈ-M Futures).

Estrategia eficiente (opción 1): el universo se limita a los símbolos MÁS LÍQUIDOS
por volumen 24h (`quoteVolume` de `/fapi/v1/ticker/24hr`, UNA sola llamada de peso
~40). El cambio % a 24h viene directo de ese endpoint. Para otras ventanas
(1h/4h/5d/1month) se calcula desde klines SOLO de ese subconjunto líquido, en
paralelo y con concurrencia limitada para no disparar el rate-limit.

Futuros no expone un ticker de ventana móvil arbitraria, por eso las ventanas
distintas a 24h se derivan de klines.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable, Optional

# Ventana -> (intervalo de kline, nº de velas que cubren la ventana).
WINDOWS = {
    "1h": ("1m", 60),
    "4h": ("5m", 48),
    "1d": ("15m", 96),     # 24h; equivalente al cambio del ticker, pero como klines
    "5d": ("1h", 120),
    "1month": ("4h", 180),
}


@dataclass
class Ticker:
    symbol: str
    last: float
    change_pct: float   # 24h
    quote_volume: float  # volumen en USDT (liquidez)


@dataclass
class Mover:
    symbol: str
    last: float
    change_pct: float   # cambio % de la ventana solicitada
    quote_volume: float


@dataclass
class MarketSummary:
    total: int
    advancers: int
    decliners: int
    total_quote_volume: float
    avg_change: float
    top_gainer: Optional[Ticker]
    top_loser: Optional[Ticker]


def parse_tickers(rows, *, allowed: Optional[set] = None) -> list[Ticker]:
    """Filtra a símbolos permitidos (p.ej. solo PERPETUAL USDT) y normaliza."""
    out: list[Ticker] = []
    for r in rows:
        sym = r.get("symbol", "")
        if allowed is not None and sym not in allowed:
            continue
        try:
            out.append(Ticker(
                symbol=sym,
                last=float(r["lastPrice"]),
                change_pct=float(r["priceChangePercent"]),
                quote_volume=float(r["quoteVolume"]),
            ))
        except (KeyError, ValueError):
            continue
    return out


def summarize_market(tickers: Iterable[Ticker]) -> MarketSummary:
    tickers = list(tickers)
    adv = sum(1 for t in tickers if t.change_pct > 0)
    dec = sum(1 for t in tickers if t.change_pct < 0)
    vol = sum(t.quote_volume for t in tickers)
    avg = (sum(t.change_pct for t in tickers) / len(tickers)) if tickers else 0.0
    return MarketSummary(
        total=len(tickers), advancers=adv, decliners=dec,
        total_quote_volume=vol, avg_change=avg,
        top_gainer=max(tickers, key=lambda t: t.change_pct, default=None),
        top_loser=min(tickers, key=lambda t: t.change_pct, default=None),
    )


async def fetch_tickers(client, *, allowed: Optional[set] = None) -> list[Ticker]:
    rows = await client.ticker_24h()  # todos los símbolos (1 llamada)
    return parse_tickers(rows, allowed=allowed)


async def _window_change(client, symbol, interval, count, sem) -> Optional[float]:
    async with sem:
        try:
            kl = await client.klines(symbol, interval, limit=count)
        except Exception:
            return None
    if len(kl) < 2:
        return None
    first_open = float(kl[0][1])   # open de la 1ª vela de la ventana
    last_close = float(kl[-1][4])  # close de la última (precio actual)
    if first_open == 0:
        return None
    return (last_close - first_open) / first_open * 100.0


async def top_movers(
    client, tickers: Iterable[Ticker], *, window: str = "1d", limit: int = 10,
    universe: int = 50, concurrency: int = 8,
) -> list[Mover]:
    """Top `limit` por cambio % en `window`, dentro de los `universe` más líquidos."""
    liquid = sorted(tickers, key=lambda t: t.quote_volume, reverse=True)[:universe]
    if window == "1d":
        movers = [Mover(t.symbol, t.last, t.change_pct, t.quote_volume) for t in liquid]
    else:
        if window not in WINDOWS:
            raise ValueError(f"Ventana no soportada: {window}")
        interval, count = WINDOWS[window]
        sem = asyncio.Semaphore(concurrency)
        changes = await asyncio.gather(*[
            _window_change(client, t.symbol, interval, count, sem) for t in liquid
        ])
        movers = [Mover(t.symbol, t.last, c, t.quote_volume)
                  for t, c in zip(liquid, changes) if c is not None]
    movers.sort(key=lambda m: m.change_pct, reverse=True)
    return movers[:limit]
