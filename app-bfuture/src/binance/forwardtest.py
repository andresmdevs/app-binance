"""Bot de forward-test en TESTNET para medir la estrategia con disciplina.

Encodea la entrada del usuario ("fuerza de las velas") de forma sistemática:
selecciona monedas BARATAS entre las de mayor subida (top gainers) y abre cada
operación con el motor de scalp -> entrada MAKER + TP + SL + cierre por TIEMPO,
respetando las guardas de riesgo. Tras agotar el presupuesto de operaciones, mide
la EXPECTATIVA real (PnL neto, win rate, profit factor).

HONESTO sobre testnet: esto valida que el bot opera el ciclo completo con disciplina
y sin dejar estado colgado. Los PRECIOS de testnet son sintéticos, así que la
expectativa que mida aquí NO representa producción; sirve para validar la mecánica.
La expectativa real solo sale operando en producción con tamaño mínimo (tú decides).

Uso:
  PYTHONPATH=src uv run python -m binance.forwardtest --trades 5 --max-price 0.5
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from . import market as market_mod
from . import pnl as pnl_mod
from .audit import AuditLog
from .client import BinanceError, BinanceFuturesClient
from .filters import FilterCache
from .risk import RiskError, RiskManager
from .scalp import ScalpConfig, close_scalp, open_scalp
from .trade import FuturesTrader


@dataclass
class ForwardTestConfig:
    max_price: float = 0.5                  # solo monedas baratas (<= este precio)
    min_quote_volume: float = 1_000_000.0   # liquidez 24h mínima
    top_n: int = 5                          # entre cuántos top-gainers elegir
    direction: str = "BUY"                  # momentum continuation (LONG)
    notional: Decimal = Decimal("60")
    take_profit_pct: float = 0.01
    stop_loss_pct: float = 0.015
    max_hold_seconds: int = 180
    leverage: int = 10
    maker: bool = True
    max_trades: int = 5
    max_concurrent: int = 1
    cycle_seconds: int = 20
    max_runtime_s: int = 3600               # tope de tiempo total del test (anti-bucle)


def select_candidates(tickers, valid_symbols, *, max_price, min_quote_volume,
                      top_n, exclude=(), direction="BUY"):
    """Monedas baratas y líquidas con momentum en la dirección dada.

    direction BUY -> top gainers (cambio 24h>0, desc); SELL -> top losers (<0, asc).
    """
    exclude = set(exclude)
    up = direction.upper() == "BUY"
    cands = [t for t in tickers
             if t.symbol in valid_symbols and t.symbol not in exclude
             and 0 < t.last <= max_price
             and t.quote_volume >= min_quote_volume
             and (t.change_pct > 0 if up else t.change_pct < 0)]
    cands.sort(key=lambda t: t.change_pct, reverse=up)
    return [t.symbol for t in cands[:top_n]]


async def _timeout_close(trader, client, symbol, seconds):
    await asyncio.sleep(seconds)
    try:
        await close_scalp(trader, client, symbol)
    except BinanceError:
        pass


async def run_forward_test(client, filters, trader, risk, cfg: ForwardTestConfig,
                           *, progress=None) -> dict:
    def _p(m):
        if progress:
            progress(m)

    start_ms = int(time.time() * 1000)
    start_s = time.time()
    valid = filters.symbols()
    opened = 0
    cycles = 0
    timeout_tasks: list = []

    while opened < cfg.max_trades and (time.time() - start_s) < cfg.max_runtime_s:
        cycles += 1
        positions = [p for p in await client.position_risk()
                     if float(p.get("positionAmt", 0)) != 0]
        held = {p["symbol"] for p in positions}
        if len(held) >= cfg.max_concurrent:
            await asyncio.sleep(cfg.cycle_seconds)
            continue

        tickers = await market_mod.fetch_tickers(client, allowed=valid)
        cands = select_candidates(
            tickers, valid, max_price=cfg.max_price,
            min_quote_volume=cfg.min_quote_volume, top_n=cfg.top_n, exclude=held)
        if not cands:
            _p(f"[ciclo {cycles}] sin candidatos baratos con momentum; espero…")
            await asyncio.sleep(cfg.cycle_seconds)
            continue

        sym = cands[0]
        try:
            await risk.check_open(sym, cfg.notional)
        except RiskError as e:
            _p(f"[ciclo {cycles}] {sym} bloqueado por riesgo: {e}")
            await asyncio.sleep(cfg.cycle_seconds)
            continue

        scfg = ScalpConfig(
            notional=cfg.notional, take_profit_pct=cfg.take_profit_pct,
            stop_loss_pct=cfg.stop_loss_pct, max_hold_seconds=cfg.max_hold_seconds,
            leverage=min(cfg.leverage, risk.limits.max_leverage),
            entry_type="MAKER" if cfg.maker else "MARKET", entry_timeout_s=15)
        try:
            res = await open_scalp(trader, client, symbol=sym, side=cfg.direction,
                                   config=scfg)
        except BinanceError as e:
            _p(f"[ciclo {cycles}] fallo abriendo {sym}: {e}")
            await asyncio.sleep(cfg.cycle_seconds)
            continue

        if not res.get("filled"):
            _p(f"[ciclo {cycles}] {sym}: entrada maker no llenó; reintento luego")
            await asyncio.sleep(cfg.cycle_seconds)
            continue

        opened += 1
        _p(f"[{opened}/{cfg.max_trades}] ABIERTA {sym} {cfg.direction} "
           f"TP+{cfg.take_profit_pct*100:g}% SL-{cfg.stop_loss_pct*100:g}% "
           f"bracket={'ok' if not res['errors'] else res['errors']}")
        timeout_tasks.append(asyncio.create_task(
            _timeout_close(trader, client, sym, cfg.max_hold_seconds)))
        await asyncio.sleep(cfg.cycle_seconds)

    _p("Presupuesto agotado; esperando cierre de posiciones (TP/SL/tiempo)…")
    if timeout_tasks:
        await asyncio.gather(*timeout_tasks, return_exceptions=True)
    # Limpieza de seguridad: cerrar lo que quede.
    for p in [p for p in await client.position_risk() if float(p.get("positionAmt", 0)) != 0]:
        await close_scalp(trader, client, p["symbol"])
    await asyncio.sleep(2.0)  # dejar asentar el PnL realizado

    end_ms = int(time.time() * 1000)
    events = await pnl_mod.fetch_realized_pnl(client, start_ms=start_ms, end_ms=end_ms)
    summary = pnl_mod.summarize(events)
    return {"opened": opened, "cycles": cycles, "events": events, "summary": summary}


def _load_keys():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    return os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET")


def parse_args():
    p = argparse.ArgumentParser(description="Forward-test del scalp en TESTNET.")
    p.add_argument("--trades", type=int, default=5)
    p.add_argument("--max-price", type=float, default=0.5)
    p.add_argument("--min-vol", type=float, default=1_000_000.0)
    p.add_argument("--notional", type=float, default=60.0)
    p.add_argument("--tp", type=float, default=1.0, help="take-profit %")
    p.add_argument("--sl", type=float, default=1.5, help="stop-loss %")
    p.add_argument("--hold", type=int, default=180, help="cierre por tiempo (s)")
    p.add_argument("--leverage", type=int, default=10)
    p.add_argument("--cycle", type=int, default=20)
    p.add_argument("--concurrent", type=int, default=1, help="posiciones simultáneas")
    p.add_argument("--runtime", type=int, default=3600, help="tope de tiempo total (s)")
    p.add_argument("--taker", action="store_true", help="usar MARKET en vez de maker")
    return p.parse_args()


async def _amain(a):
    key, sec = _load_keys()
    if not key or not sec:
        print("Faltan claves de testnet en .env.")
        return
    print("=" * 60)
    print("FORWARD-TEST · TESTNET · scalp momentum con TP/SL/tiempo")
    print("=" * 60)
    cli = BinanceFuturesClient(key, sec, testnet=True)
    if not cli.testnet:
        print("ABORTO: no es testnet.")
        return
    audit = AuditLog(Path(__file__).resolve().parents[2] / "logs" / "forwardtest.jsonl")
    cfg = ForwardTestConfig(
        max_price=a.max_price, min_quote_volume=a.min_vol, notional=Decimal(str(a.notional)),
        take_profit_pct=a.tp / 100.0, stop_loss_pct=a.sl / 100.0, max_hold_seconds=a.hold,
        leverage=a.leverage, maker=not a.taker, max_trades=a.trades, cycle_seconds=a.cycle,
        max_concurrent=a.concurrent, max_runtime_s=a.runtime)
    async with cli:
        await cli.sync_time()
        filters = FilterCache(cli)
        await filters.refresh()
        trader = FuturesTrader(cli, filters, audit=audit)
        risk = RiskManager(cli)
        res = await run_forward_test(cli, filters, trader, risk, cfg,
                                     progress=lambda m: print("  ·", m, flush=True))

    s = res["summary"]
    print("\n--- RESULTADO (condiciones de TESTNET, no representa producción) ---")
    if s.count == 0:
        print(f"Operaciones abiertas: {res['opened']} · sin PnL realizado en la ventana.")
        return
    pf = "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
    print(f"Operaciones cerradas: {s.count} · ganadoras {s.wins} ({s.win_rate*100:.0f}%)")
    print(f"PnL neto: {s.total:+.4f} USDT · PF {pf} · mejor {s.best:+.4f} / peor {s.worst:+.4f}")
    print("Recuerda: la expectativa REAL solo se mide en producción con tamaño mínimo.")


def main():
    asyncio.run(_amain(parse_args()))


if __name__ == "__main__":
    main()
