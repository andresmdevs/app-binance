"""CLI del backtester.

Ejemplos:
  uv run --group backtest python -m backtest
  uv run --group backtest python -m backtest --symbols BTCUSDT ETHUSDT --interval 1h --strategy ema_atr
  uv run --group backtest python -m backtest --strategy rsi_rev --days 365 --plot
  uv run --group backtest python -m backtest --start 2023-01-01 --end 2024-01-01 --interval 4h
"""
from __future__ import annotations

import argparse

import pandas as pd

from .data import get_klines
from .engine import BTConfig, run_backtest
from .metrics import compute_metrics, format_report
from .strategy import STRATEGIES


def parse_args():
    p = argparse.ArgumentParser(description="Backtester de futuros USDⓈ-M (BTC/ETH).")
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--interval", default="1h")
    p.add_argument("--strategy", default="ema_atr", choices=list(STRATEGIES))
    p.add_argument("--days", type=int, default=540, help="ventana hacia atrás si no se da --start")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--risk", type=float, default=0.01, help="fracción del equity por trade (0.01 = 1%%)")
    p.add_argument("--leverage", type=float, default=5.0)
    p.add_argument("--fee", type=float, default=0.0004)
    p.add_argument("--slippage-bps", type=float, default=1.0)
    p.add_argument("--sl-atr", type=float, default=2.0)
    p.add_argument("--tp-atr", type=float, default=3.0)
    p.add_argument("--no-tp", action="store_true")
    p.add_argument("--no-short", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--plot", action="store_true")
    return p.parse_args()


def main():
    a = parse_args()
    end = (pd.Timestamp(a.end, tz="UTC") if a.end else pd.Timestamp.now(tz="UTC")).floor("h")
    start = (pd.Timestamp(a.start, tz="UTC") if a.start else end - pd.Timedelta(days=a.days)).floor("D")

    cfg = BTConfig(
        initial_capital=a.capital, risk_pct=a.risk, fee_rate=a.fee,
        slippage_bps=a.slippage_bps, leverage_cap=a.leverage,
        atr_mult_sl=a.sl_atr, atr_mult_tp=a.tp_atr,
        use_tp=not a.no_tp, allow_short=not a.no_short,
    )
    strat = STRATEGIES[a.strategy]()

    print(f"\n=== Backtest: {strat.name} | {a.interval} | {start.date()} → {end.date()} "
          f"| capital ${a.capital:,.0f} | riesgo {a.risk*100:.1f}%/trade | "
          f"lev x{a.leverage:.0f} | fee {a.fee*100:.3f}% | SL {a.sl_atr}·ATR TP {a.tp_atr}·ATR ===")

    results = {}
    for sym in a.symbols:
        df = get_klines(sym, a.interval, start, end, use_cache=not a.no_cache)
        trades, eq = run_backtest(strat.prepare(df), cfg)
        m = compute_metrics(trades, eq, a.interval, cfg)
        print(format_report(m, f"{sym}  ·  {strat.name}"))
        results[sym] = (df, eq)

    print("\nReferencia — comprar y mantener (buy & hold) en el mismo periodo:")
    for sym, (df, _) in results.items():
        bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        print(f"  {sym}: {bh:+.2f}%")

    print("\nRecordatorio: el backtest NO modela funding de perpetuos. Una estrategia que "
          "mantiene posiciones muchas horas pagará/cobrará funding cada 8h en real.")

    if a.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots(figsize=(11, 5))
            for sym, (_, eq) in results.items():
                ax.plot(eq.index, eq.values, label=f"{sym} ({strat.name})")
            ax.set_title(f"Curva de equity · {strat.name} · {a.interval}")
            ax.set_ylabel("Equity (USDT)")
            ax.grid(alpha=0.3)
            ax.legend()
            fig.tight_layout()
            out = f"backtest/equity_{strat.name}_{a.interval}.png"
            fig.savefig(out, dpi=120)
            print(f"\nGráfico guardado en {out}")
        except ImportError:
            print("\n(matplotlib no disponible; omito el gráfico.)")


if __name__ == "__main__":
    main()
