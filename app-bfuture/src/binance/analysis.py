"""Análisis de operaciones cerradas (read-only) para refinar estrategia.

Reconstruye operaciones "round-trip" (entrada→salida) a partir de los fills de
`userTrades`, calcula el PnL NETO (realizado − comisiones ± funding), la duración
(scalp vs swing), dirección y ROI, y opcionalmente superpone EMA/RSI/tendencia en
el momento de la entrada (con klines) para ver con qué condiciones técnicas
coincidieron las operaciones ganadoras.

Solo usa endpoints de LECTURA: income, userTrades, klines. NUNCA envía órdenes.

Nota: la comisión se asume en USDT (USDⓈ-M); si pagas comisiones en BNB el cálculo
de neto sería una aproximación. La reconstrucción asume operaciones que abren y
cierran (los flips dentro de un mismo fill no se modelan con precisión).
"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass
from typing import Optional

from . import pnl as pnl_mod

_WEEK_MS = 7 * 24 * 60 * 60 * 1000


@dataclass
class RoundTrip:
    symbol: str
    direction: str          # LONG / SHORT
    open_time: int          # ms
    close_time: int         # ms
    duration_s: float
    qty: float
    entry: float
    exit: float
    realized_pnl: float
    commission: float
    funding: float
    net_pnl: float          # realized - commission + funding
    notional: float
    roi_pct: float          # neto sobre el notional (×apalancamiento ≈ ROI sobre margen)
    entry_trend: Optional[str] = None    # "up"/"down" (EMA rápida vs lenta)
    entry_rsi: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    aligned: Optional[bool] = None       # entró a favor de la tendencia EMA
    liquidated: bool = False             # cerrada por liquidación forzada


# --- Indicadores (plain python, sin pandas) ----------------------------------
def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_last(values: list[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


# --- Reconstrucción de operaciones -------------------------------------------
def _finalize(acc: dict, fund: list[tuple[int, float]]) -> RoundTrip:
    entry = acc["entry_notional"] / acc["entry_qty"] if acc["entry_qty"] else 0.0
    exit_ = acc["exit_notional"] / acc["exit_qty"] if acc["exit_qty"] else 0.0
    funding = sum(inc for ts, inc in fund if acc["open_time"] <= ts <= acc["close_time"])
    net = acc["realized"] - acc["commission"] + funding
    notional = acc["entry_notional"]
    dur = (acc["close_time"] - acc["open_time"]) / 1000.0
    return RoundTrip(
        symbol=acc["symbol"], direction=acc["direction"],
        open_time=acc["open_time"], close_time=acc["close_time"], duration_s=dur,
        qty=acc["entry_qty"], entry=entry, exit=exit_,
        realized_pnl=acc["realized"], commission=acc["commission"], funding=funding,
        net_pnl=net, notional=notional,
        roi_pct=(net / notional * 100.0) if notional else 0.0,
    )


def reconstruct_trades(trades: list[dict], funding_events=None) -> list[RoundTrip]:
    """Agrupa fills de userTrades en operaciones cerradas (por símbolo)."""
    trades = sorted(trades, key=lambda t: (int(t["time"]), int(t.get("id", 0))))
    fund = sorted((int(f["time"]), float(f["income"])) for f in (funding_events or []))
    trips: list[RoundTrip] = []
    pos = 0.0
    acc: Optional[dict] = None
    for t in trades:
        q = float(t["qty"]); price = float(t["price"]); ts = int(t["time"])
        comm = float(t.get("commission", 0) or 0)
        rp = float(t.get("realizedPnl", 0) or 0)
        signed = q if t["side"] == "BUY" else -q
        if acc is None or pos == 0.0:
            acc = {"symbol": t["symbol"], "direction": "LONG" if signed > 0 else "SHORT",
                   "open_time": ts, "close_time": ts,
                   "entry_notional": q * price, "entry_qty": q,
                   "exit_notional": 0.0, "exit_qty": 0.0,
                   "commission": comm, "realized": rp}
            pos = signed
            continue
        acc["commission"] += comm
        acc["realized"] += rp
        if (pos > 0 and signed > 0) or (pos < 0 and signed < 0):  # añade a la posición
            acc["entry_notional"] += q * price
            acc["entry_qty"] += q
        else:                                                      # reduce/cierra
            acc["exit_notional"] += q * price
            acc["exit_qty"] += q
        pos += signed
        if abs(pos) < 1e-12:
            acc["close_time"] = ts
            trips.append(_finalize(acc, fund))
            acc, pos = None, 0.0
    return trips


# --- Acceso a la API (lectura) -----------------------------------------------
async def _user_trades_window(client, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        chunk_end = min(cur + _WEEK_MS, end_ms)
        batch = await client.user_trades(symbol, limit=1000, startTime=cur, endTime=chunk_end)
        out.extend(batch)
        if len(batch) >= 1000:
            cur = int(batch[-1]["time"]) + 1
        else:
            cur = chunk_end
    return out


async def enrich_with_indicators(
    client, trips: list[RoundTrip], *, interval: str = "5m", lookback: int = 120,
    fast: int = 9, slow: int = 21, concurrency: int = 6,
) -> None:
    sem = asyncio.Semaphore(concurrency)

    async def one(tr: RoundTrip):
        async with sem:
            try:
                kl = await client.klines(tr.symbol, interval, limit=lookback, endTime=tr.open_time)
            except Exception:
                return
        closes = [float(k[4]) for k in kl]
        if len(closes) < slow + 1:
            return
        tr.ema_fast = ema_series(closes, fast)[-1]
        tr.ema_slow = ema_series(closes, slow)[-1]
        tr.entry_trend = "up" if tr.ema_fast > tr.ema_slow else "down"
        tr.entry_rsi = rsi_last(closes, 14)
        tr.aligned = ((tr.direction == "LONG" and tr.entry_trend == "up")
                      or (tr.direction == "SHORT" and tr.entry_trend == "down"))

    await asyncio.gather(*[one(t) for t in trips])


async def _force_orders_window(client, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        chunk_end = min(cur + _WEEK_MS, end_ms)
        batch = await client.force_orders(
            symbol=symbol, auto_close_type="LIQUIDATION",
            startTime=cur, endTime=chunk_end, limit=100)
        out.extend(batch)
        cur = chunk_end
    return out


async def analyze(
    client, *, months: float = 6, only_positive: bool = False,
    with_indicators: bool = True, progress=None,
) -> list[RoundTrip]:
    """Reconstruye las operaciones cerradas de los últimos `months` meses y marca
    las liquidadas (forceOrders solo cubre ~90 días).

    ``progress``: callback opcional ``fn(msg: str)`` para reportar avance.
    """
    def _p(msg: str) -> None:
        if progress:
            progress(msg)

    end = int(time.time() * 1000)
    start = end - int(months * 30 * 24 * 60 * 60 * 1000)
    # Descubrir símbolos operados a partir del PnL realizado.
    _p("Descubriendo símbolos operados (income REALIZED_PNL)…")
    events = await pnl_mod.fetch_realized_pnl(client, start_ms=start, end_ms=end)
    symbols = sorted({e.symbol for e in events if e.symbol})
    _p(f"Símbolos operados: {len(symbols)} -> {', '.join(symbols) if symbols else '(ninguno)'}")

    # Funding de TODA la cuenta de una vez (sin symbol) -> mucho menos rate-limit.
    _p("Descargando funding de toda la cuenta…")
    fund_by_sym: dict[str, list] = {}
    for r in await pnl_mod._fetch_income(client, "FUNDING_FEE", symbol=None,
                                         start_ms=start, end_ms=end):
        fund_by_sym.setdefault(r.get("symbol", ""), []).append(r)

    trips: list[RoundTrip] = []
    for i, sym in enumerate(symbols, 1):
        trades = await _user_trades_window(client, sym, start, end)
        new = reconstruct_trades(trades, fund_by_sym.get(sym, []))
        trips.extend(new)
        _p(f"[{i}/{len(symbols)}] {sym}: {len(trades)} fills -> {len(new)} operaciones")

    # Liquidaciones de TODA la cuenta (forceOrders sin symbol, ≤90 días).
    _p("Buscando liquidaciones (forceOrders, ≤90 días)…")
    liq_start = max(start, end - 90 * 24 * 60 * 60 * 1000)
    liq_by_sym: dict[str, list] = {}
    for o in await _force_orders_window(client, None, liq_start, end):
        liq_by_sym.setdefault(o.get("symbol", ""), []).append(int(o["time"]))
    liq_total = 0
    for t in trips:
        for ft in liq_by_sym.get(t.symbol, []):
            if t.open_time <= ft <= t.close_time + 3000:
                t.liquidated = True
                liq_total += 1
                break
    _p(f"Liquidaciones detectadas: {liq_total}")

    if only_positive:
        trips = [t for t in trips if t.net_pnl > 0]
    trips.sort(key=lambda t: t.close_time)
    if with_indicators and trips:
        _p(f"Enriqueciendo {len(trips)} operaciones con EMA/RSI (klines)…")
        await enrich_with_indicators(client, trips)
    _p("Listo.")
    return trips


def _seg(trips: list[RoundTrip]) -> dict:
    """Estadísticas de un subconjunto (ganadoras / perdedoras / liquidadas)."""
    if not trips:
        return {"count": 0}
    nets = [t.net_pnl for t in trips]
    durs = [t.duration_s for t in trips]
    enr = [t for t in trips if t.aligned is not None]
    rsis = [t.entry_rsi for t in trips if t.entry_rsi is not None]
    return {
        "count": len(trips),
        "net": sum(nets),
        "avg_net": statistics.mean(nets),
        "avg_hold_s": statistics.mean(durs),
        "median_hold_s": statistics.median(durs),
        "avg_notional": statistics.mean([t.notional for t in trips]),
        "aligned_pct": (sum(1 for t in enr if t.aligned) / len(enr)) if enr else None,
        "avg_entry_rsi": statistics.mean(rsis) if rsis else None,
    }


def summarize_trades(trips: list[RoundTrip]) -> dict:
    n = len(trips)
    if n == 0:
        return {"count": 0}
    winners = [t for t in trips if t.net_pnl > 0]
    losers = [t for t in trips if t.net_pnl <= 0]
    liq = [t for t in trips if t.liquidated]
    nets = [t.net_pnl for t in trips]
    gross_profit = sum(t.net_pnl for t in winners)
    gross_loss = -sum(t.net_pnl for t in losers)
    by_symbol: dict[str, dict] = {}
    for t in trips:
        s = by_symbol.setdefault(t.symbol, {"count": 0, "net": 0.0, "liq": 0})
        s["count"] += 1
        s["net"] += t.net_pnl
        s["liq"] += 1 if t.liquidated else 0
    return {
        "count": n,
        "wins": len(winners), "losses": len(losers),
        "win_rate": len(winners) / n,
        "total_net": sum(nets),
        "gross_profit": gross_profit, "gross_loss": gross_loss,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else float("inf"),
        "total_commission": sum(t.commission for t in trips),
        "total_funding": sum(t.funding for t in trips),
        "scalps_lt5m": sum(1 for t in trips if t.duration_s < 300),
        "longs": sum(1 for t in trips if t.direction == "LONG"),
        "shorts": sum(1 for t in trips if t.direction == "SHORT"),
        "best": max(nets), "worst": min(nets),
        "liquidations": len(liq), "liq_loss": sum(t.net_pnl for t in liq),
        "by_symbol": by_symbol,
        "winners": _seg(winners),
        "losers": _seg(losers),
        "liquidated": _seg(liq),
    }
