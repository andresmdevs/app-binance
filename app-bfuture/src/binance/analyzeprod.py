"""Analizador de operaciones de PRODUCCIÓN (solo lectura).

Usa BINANCE_PROD_API_KEY/SECRET contra mainnet en MODO LECTURA (income, userTrades,
klines, forceOrders). NUNCA envía órdenes. La app de trading sigue en testnet.

    PYTHONPATH=src uv run python -m binance.analyzeprod [meses]
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from binance import BinanceError, BinanceFuturesClient
from binance import analysis as an


def _keys() -> tuple[str | None, str | None]:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    k = os.getenv("BINANCE_PROD_API_KEY") or os.getenv("BINANCE_API_KEY")
    s = os.getenv("BINANCE_PROD_API_SECRET") or os.getenv("BINANCE_API_SECRET")
    placeholder = {None, "", "tu_api_key", "tu_api_secret"}
    return (None, None) if (k in placeholder or s in placeholder) else (k, s)


def _dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _dur(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def _seg_line(name: str, d: dict) -> None:
    if not d.get("count"):
        print(f"  {name:<11}: 0")
        return
    al = "-" if d["aligned_pct"] is None else f"{d['aligned_pct'] * 100:.0f}%"
    rsi = "-" if d["avg_entry_rsi"] is None else f"{d['avg_entry_rsi']:.0f}"
    print(f"  {name:<11}: {d['count']:>4} · neto medio {d['avg_net']:+7.3f} · "
          f"hold med {_dur(d['median_hold_s']):>6} · a-favor-tend {al:>4} · "
          f"RSI {rsi:>3} · notional {d['avg_notional']:.1f}")


async def run(months: float) -> None:
    k, s = _keys()
    if not k or not s:
        print("Faltan BINANCE_PROD_API_KEY/SECRET en .env.")
        return
    print("=" * 66)
    print(f"ANÁLISIS DE PRODUCCIÓN · SOLO LECTURA · últimos {months:g} meses")
    print("=" * 66)
    cli = BinanceFuturesClient(k, s, testnet=False)  # PRODUCCIÓN, solo lectura
    try:
        async with cli:
            await cli.sync_time()
            trips = await an.analyze(cli, months=months, only_positive=False,
                                     with_indicators=True,
                                     progress=lambda m: print("  ·", m, flush=True))
    except BinanceError as e:
        print(f"Error consultando producción: {e}")
        print("(Si es -2015, la API key anterior ya no sirve o no tiene permiso de lectura.)")
        return

    if not trips:
        print("No se encontraron operaciones cerradas en el periodo.")
        return

    s_ = an.summarize_trades(trips)
    pf = "∞" if s_["profit_factor"] == float("inf") else f"{s_['profit_factor']:.2f}"
    print(f"\nOperaciones cerradas: {s_['count']} · ganadoras {s_['wins']} "
          f"({s_['win_rate'] * 100:.0f}%) · perdedoras {s_['losses']}")
    print(f"PnL NETO total: {s_['total_net']:+.2f} USDT "
          f"(ganan {s_['gross_profit']:.2f} / pierden {s_['gross_loss']:.2f} · PF {pf})")
    print(f"Comisiones: {s_['total_commission']:.2f} · Funding: {s_['total_funding']:+.2f} USDT")
    print(f"Scalps (<5min): {s_['scalps_lt5m']} · Long {s_['longs']} / Short {s_['shorts']}")
    print(f"Mejor: {s_['best']:+.2f} · Peor: {s_['worst']:+.2f}")
    print(f"⚠ LIQUIDACIONES (≤90d): {s_['liquidations']} · pérdida {s_['liq_loss']:+.2f} USDT")

    print("\nComparativa (clave para encontrar/evitar patrones):")
    _seg_line("Ganadoras", s_["winners"])
    _seg_line("Perdedoras", s_["losers"])
    _seg_line("Liquidadas", s_["liquidated"])

    print("\nPor símbolo (top 10 por |neto|):")
    for sym, info in sorted(s_["by_symbol"].items(), key=lambda kv: -abs(kv[1]["net"]))[:10]:
        liq = f" · {info['liq']} liq" if info["liq"] else ""
        print(f"  {sym:<12} {info['count']:>3} ops · neto {info['net']:+8.2f}{liq}")

    liqs = [t for t in trips if t.liquidated]
    if liqs:
        print(f"\nLIQUIDACIONES detalle (últimas {min(len(liqs), 15)}):")
        for t in liqs[-15:]:
            rsi = round(t.entry_rsi) if t.entry_rsi is not None else "-"
            print(f"  {_dt(t.open_time)} {t.symbol:<11} {t.direction:<5} "
                  f"hold {_dur(t.duration_s):>6} notional {t.notional:>7.1f} "
                  f"net {t.net_pnl:+7.2f} tend {t.entry_trend or '-':<4} RSI {rsi}")

    print("\nPeores pérdidas (top 10):")
    for t in sorted(trips, key=lambda x: x.net_pnl)[:10]:
        tag = " [LIQ]" if t.liquidated else ""
        print(f"  {_dt(t.open_time)} {t.symbol:<11} {t.direction:<5} "
              f"hold {_dur(t.duration_s):>6} net {t.net_pnl:+7.2f}{tag}")


if __name__ == "__main__":
    months = float(sys.argv[1]) if len(sys.argv) > 1 else 6
    asyncio.run(run(months))
