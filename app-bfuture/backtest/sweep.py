"""Barrido de parámetros del scalp (TP/SL fijos en % + cierre por tiempo).

Responde la pregunta clave del análisis: ¿una salida disciplinada (TP/SL fijos +
timeout) sobre una entrada sistemática simple tiene expectativa positiva DESPUÉS de
comisiones? Recorre combinaciones y las ordena por profit factor.

Uso:
  uv run --group backtest python -m backtest.sweep
  uv run --group backtest python -m backtest.sweep --interval 1m --days 90 --leverage 20 \
      --tp 0.3 0.5 0.8 1.2 --sl 0.3 0.5 0.8 --hold 3 5 10

Advertencias honestas:
- Es un PROXY en BTC/ETH (datos fiables). Los alts baratos que operas tienen spread
  y slippage MAYORES -> en real esto sale peor.
- El backtest no modela el funding; para scalps de minutos es despreciable.
- En klines de 1m no se ve la microestructura intra-vela: trátalo como cota
  superior optimista, y valida en testnet con el motor de scalp real.
"""
from __future__ import annotations

import argparse
import itertools

import pandas as pd

from .data import get_klines
from .engine import BTConfig, run_backtest
from .strategy import STRATEGIES


def parse_args():
    p = argparse.ArgumentParser(description="Barrido TP/SL/tiempo del scalp.")
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--interval", default="1m")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--strategy", default="ema_atr", choices=list(STRATEGIES))
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--leverage", type=float, default=20.0)
    p.add_argument("--risk", type=float, default=0.02)
    p.add_argument("--fee", type=float, default=0.0004)
    p.add_argument("--slippage-bps", type=float, default=1.0)
    p.add_argument("--tp", nargs="+", type=float, default=[0.3, 0.5, 0.8, 1.2])  # %
    p.add_argument("--sl", nargs="+", type=float, default=[0.3, 0.5, 0.8])       # %
    p.add_argument("--hold", nargs="+", type=int, default=[3, 5, 10])            # velas
    return p.parse_args()


def main():
    a = parse_args()
    end = pd.Timestamp.now(tz="UTC").floor("h")
    start = end - pd.Timedelta(days=a.days)
    strat = STRATEGIES[a.strategy]()

    print(f"Cargando datos {a.interval} {a.days}d para {', '.join(a.symbols)} …")
    prepared = {s: strat.prepare(get_klines(s, a.interval, start, end)) for s in a.symbols}

    rows = []
    for tp, sl, hold in itertools.product(a.tp, a.sl, a.hold):
        trades_n = wins = 0
        gross_win = gross_loss = 0.0
        worst_dd = 0.0
        net = 0.0
        for dfp in prepared.values():
            cfg = BTConfig(
                initial_capital=a.capital, risk_pct=a.risk, fee_rate=a.fee,
                slippage_bps=a.slippage_bps, leverage_cap=a.leverage,
                mode="pct", pct_tp=tp / 100.0, pct_sl=sl / 100.0,
                max_hold_bars=hold, use_tp=True)
            trades, eq = run_backtest(dfp, cfg)
            for t in trades:
                trades_n += 1
                if t.pnl > 0:
                    wins += 1
                    gross_win += t.pnl
                else:
                    gross_loss += -t.pnl
            net += float(eq.iloc[-1]) - a.capital
            dd = float((eq / eq.cummax() - 1).min()) * 100
            worst_dd = min(worst_dd, dd)
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
        wr = wins / trades_n * 100 if trades_n else 0.0
        exp = (gross_win - gross_loss) / trades_n if trades_n else 0.0
        rows.append((tp, sl, hold, trades_n, wr, pf, exp, worst_dd, net))

    rows.sort(key=lambda r: (-(r[5] if r[5] != float("inf") else 1e9), -r[6]))
    print(f"\nBarrido scalp · {a.strategy} · {a.interval} · {a.days}d · "
          f"{','.join(a.symbols)} · lev x{a.leverage:.0f} · fee {a.fee * 100:.3f}%/lado")
    print(f"{'TP%':>5}{'SL%':>6}{'hold':>6}{'trades':>8}{'win%':>7}{'PF':>7}"
          f"{'exp$':>9}{'maxDD%':>9}{'netΣ$':>10}")
    for r in rows:
        pf = "inf" if r[5] == float("inf") else f"{r[5]:.2f}"
        print(f"{r[0]:>5}{r[1]:>6}{r[2]:>6}{r[3]:>8}{r[4]:>7.1f}{pf:>7}"
              f"{r[6]:>9.3f}{r[7]:>9.1f}{r[8]:>10.1f}")
    print("\nLeer sin engañarse: PF<1 = pierde. El roundtrip taker cuesta ~"
          f"{a.fee * 200:.2f}%+slippage, que se come los TP pequeños. Proxy optimista "
          "en BTC/ETH; en alts baratos será peor. Validar en testnet.")


if __name__ == "__main__":
    main()
