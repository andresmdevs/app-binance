"""Métricas de desempeño a partir de los trades y la curva de equity."""
from __future__ import annotations

import numpy as np
import pandas as pd

_BARS_PER_YEAR = {
    "1m": 525_600, "3m": 175_200, "5m": 105_120, "15m": 35_040,
    "30m": 17_520, "1h": 8_760, "2h": 4_380, "4h": 2_190,
    "6h": 1_460, "8h": 1_095, "12h": 730, "1d": 365,
}


def compute_metrics(trades, equity: pd.Series, interval: str, cfg) -> dict:
    init = cfg.initial_capital
    final = float(equity.iloc[-1])
    ret_pct = (final / init - 1) * 100

    roll_max = equity.cummax()
    max_dd = float((equity / roll_max - 1.0).min()) * 100

    bar_ret = equity.pct_change(fill_method=None).dropna()
    bpy = _BARS_PER_YEAR.get(interval, 8_760)
    sharpe = float(bar_ret.mean() / bar_ret.std() * np.sqrt(bpy)) if bar_ret.std() > 0 else 0.0

    years = (equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86_400)
    cagr = ((final / init) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else float("nan")

    pnls = np.array([t.pnl for t in trades]) if trades else np.array([])
    nt = len(pnls)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()

    reasons: dict = {}
    total_fees = 0.0
    durations = []
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
        total_fees += t.fees
        durations.append((t.exit_time - t.entry_time).total_seconds() / 3600)

    return {
        "final_equity": final,
        "return_pct": ret_pct,
        "cagr_pct": cagr,
        "max_drawdown_pct": max_dd,
        "sharpe": sharpe,
        "trades": nt,
        "win_rate_pct": (len(wins) / nt * 100) if nt else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy": float(pnls.mean()) if nt else 0.0,
        "avg_r": float(np.mean([t.r_multiple for t in trades])) if trades else 0.0,
        "exit_reasons": reasons,
        "total_fees": total_fees,
        "avg_hold_h": float(np.mean(durations)) if durations else 0.0,
    }


def format_report(m: dict, title: str) -> str:
    out = [f"\n┌─ {title} " + "─" * max(2, 48 - len(title))]

    def row(label, val):
        out.append(f"│ {label:<22} {val}")

    pf = m["profit_factor"]
    row("Retorno total", f"{m['return_pct']:+.2f}%")
    row("Equity final", f"${m['final_equity']:,.2f}")
    row("CAGR", f"{m['cagr_pct']:+.2f}%")
    row("Max drawdown", f"{m['max_drawdown_pct']:.2f}%")
    row("Sharpe (anual.)", f"{m['sharpe']:.2f}")
    row("Nº de trades", f"{m['trades']}")
    row("Win rate", f"{m['win_rate_pct']:.1f}%")
    row("Profit factor", f"{pf:.2f}" if np.isfinite(pf) else "∞")
    row("Expectancy/trade", f"${m['expectancy']:+.2f}")
    row("R medio", f"{m['avg_r']:+.2f}R")
    row("Hold medio", f"{m['avg_hold_h']:.1f} h")
    row("Comisiones totales", f"${m['total_fees']:,.2f}")
    row("Salidas", ", ".join(f"{k}:{v}" for k, v in m["exit_reasons"].items()) or "—")
    out.append("└" + "─" * 49)
    return "\n".join(out)
