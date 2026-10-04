"""CLI del motor autónomo (sin UI).

  replay  Mide las MISMAS reglas del motor sobre velas históricas REALES de
          mainnet (solo lectura, sin claves), con comisiones y slippage. Es la
          prueba que importa: los precios de testnet son sintéticos.
  run     Opera en TESTNET: analiza N s antes de cada vela y ejecuta al abrirla.
          Ctrl+C (o --minutes) lo detiene y cierra las posiciones del motor.

Ejemplos (desde app-bfuture/):

  PYTHONPATH=src uv run python -m binance.autotrader replay --symbols BTCUSDT SOLUSDT --days 7
  PYTHONPATH=src uv run python -m binance.autotrader run --symbols SOLUSDT DOGEUSDT --minutes 60
  PYTHONPATH=src uv run python -m binance.autotrader run --mode radar --minutes 120
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from .audit import AuditLog
from .autosignal import (VOTE_NAMES, ShadowBoard, SignalConfig, simulate,
                         summarize_sim)
from .client import BinanceFuturesClient
from .engine import (INTERVAL_MS, AutoEngine, EngineConfig, EngineError, _hhmmss,
                     radar_config)
from .filters import FilterCache
from .risk import RiskManager, limits_from_env
from .trade import FuturesTrader

ROOT = Path(__file__).resolve().parents[2]   # .../app-bfuture
ICONS = {"skip": "·", "open": "▶", "close": "■", "error": "!", "halt": "⛔",
         "status": "»", "stopped": "⏹"}


# --- Configuración común --------------------------------------------------------
def signal_config(a) -> SignalConfig:
    # Entrada maker: 0.02% + salida taker 0.05% (TP/SL/tiempo salen a mercado).
    fee = a.fee if a.fee is not None else (0.0007 if getattr(a, "maker", False) else 0.001)
    return SignalConfig(
        enabled=tuple(a.votes), min_score=a.min_score, tp_atr=a.tp_atr, sl_atr=a.sl_atr,
        fee_rt=fee, min_edge_fees=a.min_edge)


def engine_config(a) -> EngineConfig:
    common = dict(
        margin_usdt=Decimal(str(a.margin)), leverage=a.leverage, max_trades=a.max_trades,
        max_consecutive_losses=a.max_losses, max_concurrent=a.concurrent,
        entry_type="MAKER" if a.maker else "MARKET", signal=signal_config(a))
    if a.mode == "radar":
        return radar_config(symbols=a.symbols or [], **common)
    return EngineConfig(symbols=a.symbols or ["BTCUSDT", "ETHUSDT"], interval=a.interval,
                        lead_s=a.lead, hold_candles=a.hold, trailing=a.trailing, **common)


def _print_event(evt: dict, quiet: bool = False) -> None:
    if quiet and evt["kind"] == "skip":
        return
    print(f"{_hhmmss(evt['ts'])} {ICONS.get(evt['kind'], ' ')} {evt['msg']}", flush=True)


def _print_shadow(shadow: ShadowBoard) -> None:
    rows = [r for r in shadow.ranking() if r["total"]]
    if not rows:
        print("  (sin velas resueltas todavía)")
        return
    for r in rows:
        print(f"  {r['label']:<22} {r['rate'] * 100:5.1f}%  "
              f"[{r['lo'] * 100:4.1f}–{r['hi'] * 100:4.1f}%]  n={r['total']}")


# --- run (testnet) --------------------------------------------------------------
async def _run(a) -> int:
    load_dotenv(ROOT / ".env")
    key, sec = os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET")
    if not key or not sec:
        print("Faltan BINANCE_API_KEY / BINANCE_API_SECRET de TESTNET en .env")
        return 1
    cfg = engine_config(a)
    cli = BinanceFuturesClient(key, sec, testnet=True)   # forzado: nunca producción
    scan = BinanceFuturesClient(testnet=False) if cfg.mode == "radar" else None
    logs = ROOT / "logs"
    print("=" * 70)
    print("MOTOR AUTÓNOMO · TESTNET · Ctrl+C para detener (cierra sus posiciones)")
    print("=" * 70)
    async with cli:
        await cli.sync_time()
        filters = FilterCache(cli)
        await filters.refresh()
        trader = FuturesTrader(cli, filters, audit=AuditLog(logs / "autotrader-orders.jsonl"))
        eng = AutoEngine(cli, trader, RiskManager(cli, limits_from_env()), cfg,
                         scan_client=scan, journal=AuditLog(logs / "autotrader.jsonl"),
                         on_event=lambda e: _print_event(e, a.quiet))
        try:
            eng.start()
        except EngineError as e:
            print(f"No arranca: {e}")
            return 1
        end = time.time() + a.minutes * 60 if a.minutes else None
        try:
            while eng.running and (end is None or time.time() < end):
                await asyncio.sleep(1)
        finally:
            await eng.stop(close_positions=True, reason="fin de la sesión")
            try:
                await asyncio.wait_for(eng.join(), timeout=60)
            except asyncio.TimeoutError:
                print("! Alguna orden no terminó de cerrarse: revisa la cuenta.")
            if scan is not None:
                await scan.close()
            st = eng.stats
            wr = "—" if st.win_rate is None else f"{st.win_rate * 100:.0f}%"
            print("\n--- SESIÓN (testnet: valida la mecánica, no la rentabilidad) ---")
            print(f"Órdenes {st.opened} · cerradas {st.closed} · ganadas {st.wins} / "
                  f"perdidas {st.losses} ({wr}) · neto {st.net:+.4f} USDT · "
                  f"comisiones {st.commission:.4f} · peor racha {st.max_consec_losses}")
            print("Marcador en sombra (dirección de la vela siguiente):")
            _print_shadow(eng.shadow)
            print(f"Diario: {logs / 'autotrader.jsonl'}")
    return 0


# --- replay (mainnet histórico, solo lectura) --------------------------------------
async def fetch_history(cli, symbol: str, interval: str, days: float) -> list:
    """Velas CERRADAS de los últimos `days` días (paginado de 1500 en 1500)."""
    iv = INTERVAL_MS[interval]
    end = int(time.time() * 1000) // iv * iv           # excluye la vela en curso
    cur = end - int(days * 86_400_000)
    out: list = []
    while cur < end:
        batch = await cli.klines(symbol, interval, limit=1500, startTime=cur, endTime=end - 1)
        if not batch:
            break
        out.extend(batch)
        cur = int(batch[-1][0]) + iv
        if len(batch) < 1500:
            break
    return out


def _fmt_summary(sym: str, n_bars: int, s: dict) -> str:
    if not s.get("trades"):
        return f"{sym:<12} velas {n_bars} · sin órdenes (ninguna vela pasó los gates)"
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    reasons = " / ".join(f"{k} {v}" for k, v in sorted(s["by_reason"].items()))
    return (f"{sym:<12} velas {n_bars} · órdenes {s['trades']} (L {s['longs']} / "
            f"S {s['shorts']}) · acierto {s['win_rate'] * 100:.1f}% "
            f"[{s['win_rate_lo'] * 100:.1f}–{s['win_rate_hi'] * 100:.1f}%] · "
            f"neto medio {s['avg_net_pct'] * 100:+.3f}% · PF {pf} · "
            f"peor racha {s['max_consec_losses']} · salidas {reasons} · "
            f"≈ {s['pnl_usdt']:+.2f} USDT")


async def _replay(a) -> int:
    cfg = signal_config(a)
    shadow = ShadowBoard()
    all_trades = []
    print(f"\n=== Replay · votos por vela · {a.interval} · {a.days:g} días · votos ≥ ±"
          f"{cfg.min_score} · TP {cfg.tp_atr:g}·ATR / SL {cfg.sl_atr:g}·ATR · "
          f"duración {a.hold} vela(s) · comisión {cfg.fee_rt * 100:.2f}% ida+vuelta · "
          f"slippage {a.slippage_bps:g} bps/lado ===")
    async with BinanceFuturesClient(testnet=False) as cli:   # público, sin claves
        for sym in a.symbols or ["BTCUSDT", "ETHUSDT"]:
            kl = await fetch_history(cli, sym, a.interval, a.days)
            trades = simulate(kl, cfg, hold_candles=a.hold, slippage_bps=a.slippage_bps,
                              symbol=sym, shadow=shadow)
            all_trades += trades
            print(_fmt_summary(sym, len(kl), summarize_sim(
                trades, margin=a.margin, leverage=a.leverage)))
    total = summarize_sim(all_trades, margin=a.margin, leverage=a.leverage)
    print("-" * 70)
    print(_fmt_summary("TOTAL", 0, total).replace("velas 0 · ", ""))
    print("\nMarcador en sombra (¿acierta cada indicador la dirección de la vela siguiente?):")
    _print_shadow(shadow)
    print("\nLectura honesta:")
    if not total.get("trades"):
        print("  Ninguna vela pasó los gates: el movimiento esperado no cubre las comisiones.")
    elif total["avg_net_pct"] <= 0:
        print("  Con estas reglas y costes PIERDE en el histórico. No lo lleves a real; "
              "en testnet solo sirve para validar la mecánica.")
    else:
        print("  Positivo en el histórico. Antes de creerlo: repite en otras fechas "
              "(fuera de muestra) y con más símbolos; con <300 órdenes es ruido.")
    print("  (El replay decide con el cierre de la vela; en vivo se decide 4 s antes.)")
    return 0


# --- Argumentos -------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Motor autónomo de futuros (testnet) y replay.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--symbols", nargs="+", default=None)
        sp.add_argument("--interval", default="1m", choices=list(INTERVAL_MS))
        sp.add_argument("--min-score", type=int, default=2, help="|suma de votos| mínima")
        sp.add_argument("--votes", nargs="+", default=list(VOTE_NAMES), choices=list(VOTE_NAMES),
                        help="votos que cuentan para la suma")
        sp.add_argument("--tp-atr", type=float, default=1.5)
        sp.add_argument("--sl-atr", type=float, default=1.0)
        sp.add_argument("--hold", type=int, default=1, help="duración máx (velas)")
        sp.add_argument("--fee", type=float, default=None,
                        help="comisión ida+vuelta en fracción (def. 0.001; 0.0007 con --maker)")
        sp.add_argument("--min-edge", type=float, default=2.0,
                        help="el TP debe cubrir N × la comisión ida+vuelta")
        sp.add_argument("--margin", type=float, default=5.0, help="margen por orden (USDT)")
        sp.add_argument("--leverage", type=int, default=5)

    r = sub.add_parser("run", help="opera en TESTNET")
    common(r)
    r.add_argument("--mode", choices=("candle", "radar"), default="candle")
    r.add_argument("--lead", type=float, default=4.0, help="segundos antes de la vela")
    r.add_argument("--minutes", type=float, default=0, help="duración (0 = hasta Ctrl+C)")
    r.add_argument("--max-trades", type=int, default=50)
    r.add_argument("--max-losses", type=int, default=4, help="pausa tras N pérdidas seguidas")
    r.add_argument("--concurrent", type=int, default=2)
    r.add_argument("--trailing", action="store_true")
    r.add_argument("--maker", action="store_true", help="entrada post-only (sin fee taker)")
    r.add_argument("--quiet", action="store_true", help="oculta las velas descartadas")

    rp = sub.add_parser("replay", help="mide las reglas en velas históricas de mainnet")
    common(rp)
    rp.add_argument("--days", type=float, default=7)
    rp.add_argument("--slippage-bps", type=float, default=1.0)
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        return asyncio.run(_run(a) if a.cmd == "run" else _replay(a))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
