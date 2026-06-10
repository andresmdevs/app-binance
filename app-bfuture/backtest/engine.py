"""Motor de backtesting para futuros perpetuos USDⓈ-M.

Decisiones de diseño (pensadas para NO engañarse a uno mismo):
- Ejecución en la APERTURA de la vela siguiente a la señal  → sin lookahead.
- SL/TP intra-vela; si ambos se tocan en la misma vela se asume el PEOR caso
  (se ejecuta el STOP primero).
- Comisiones taker + slippage aplicados en CADA entrada y salida.
- Una sola posición a la vez (no piramida).
- Sizing por riesgo: se arriesga `risk_pct` del equity por trade (distancia al stop),
  con tope por apalancamiento.

LIMITACIONES CONOCIDAS (ver README): NO modela el funding de perpetuos ni la
liquidación por margen de mantenimiento. Por eso los stops se mantienen muy por
dentro del nivel de liquidación vía el tope de apalancamiento.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class BTConfig:
    initial_capital: float = 1000.0
    risk_pct: float = 0.01        # fracción del equity arriesgada por trade
    fee_rate: float = 0.0004      # taker 0.04% Binance USDⓈ-M
    slippage_bps: float = 1.0     # 1 punto básico por lado
    leverage_cap: float = 5.0
    atr_mult_sl: float = 2.0
    atr_mult_tp: float = 3.0
    use_tp: bool = True
    allow_short: bool = True
    # Modo scalp: SL/TP FIJOS en % y cierre por TIEMPO (nº de velas).
    mode: str = "atr"           # "atr" | "pct"
    pct_sl: float = 0.005        # stop fijo (fracción) en modo pct
    pct_tp: float = 0.01         # target fijo (fracción) en modo pct
    max_hold_bars: int = 0       # cierre por tiempo (velas); 0 = desactivado


@dataclass
class Trade:
    side: int             # +1 long, -1 short
    entry_time: pd.Timestamp
    entry: float
    exit_time: pd.Timestamp
    exit: float
    qty: float
    pnl: float
    fees: float
    reason: str           # 'signal' | 'stop' | 'target'
    r_multiple: float


def _apply_slip(price: float, side: int, slippage_bps: float, entering: bool) -> float:
    """Empeora el precio de ejecución por slippage (en contra nuestra)."""
    slip = price * slippage_bps / 10_000.0
    if entering:
        return price + slip if side == 1 else price - slip
    return price - slip if side == 1 else price + slip


def run_backtest(df: pd.DataFrame, cfg: BTConfig):
    """df necesita columnas: open, high, low, close, atr, signal.
    Devuelve (lista de Trade, Series de equity indexada por tiempo).
    """
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    atr_v = df["atr"].values
    sig = df["signal"].values
    idx = df.index
    n = len(df)

    equity = cfg.initial_capital
    equity_curve = np.full(n, np.nan)

    pos = 0
    qty = 0.0
    entry_px = 0.0
    stop_px = 0.0
    tp_px = 0.0
    risk_per_unit = 0.0
    entry_time = None
    entry_bar = 0
    blocked_dir = 0       # dirección bloqueada tras un stop hasta que la señal cambie
    trades: list[Trade] = []

    def close_position(exit_px_raw, when, reason):
        nonlocal equity, pos, qty, entry_px, entry_time, blocked_dir
        exit_px = _apply_slip(exit_px_raw, pos, cfg.slippage_bps, entering=False)
        gross = (exit_px - entry_px) * qty * pos
        fees = (entry_px * qty + exit_px * qty) * cfg.fee_rate
        pnl = gross - fees
        equity += pnl
        dollar_risk = risk_per_unit * qty
        r = pnl / dollar_risk if dollar_risk > 0 else 0.0
        trades.append(Trade(pos, entry_time, entry_px, when, exit_px, qty, pnl,
                            fees, reason, r))
        if reason == "stop":
            blocked_dir = pos          # no re-entrar misma dirección hasta que cambie la señal
        pos = 0
        qty = 0.0

    for i in range(1, n):
        raw = sig[i - 1]
        target = 0 if np.isnan(raw) else int(raw)
        if not cfg.allow_short and target == -1:
            target = 0
        if blocked_dir != 0 and target != blocked_dir:
            blocked_dir = 0

        # --- 1) En la apertura: si la señal cambió, cerrar la posición actual ---
        if pos != 0 and target != pos:
            close_position(o[i], idx[i], "signal")

        # --- 1b) Abrir nueva posición si procede ---
        if pos == 0 and target != 0 and target != blocked_dir:
            a = atr_v[i - 1]
            side = target
            fill = _apply_slip(o[i], side, cfg.slippage_bps, entering=True)
            if cfg.mode == "pct":
                risk_per_unit = fill * cfg.pct_sl
                can_enter = risk_per_unit > 0
            else:
                risk_per_unit = cfg.atr_mult_sl * a
                can_enter = (not np.isnan(a)) and a > 0
            if can_enter:
                size = (equity * cfg.risk_pct) / risk_per_unit         # sizing por riesgo
                size = min(size, (equity * cfg.leverage_cap) / fill)   # tope por apalancamiento
                if size > 0:
                    pos, qty, entry_px, entry_time, entry_bar = side, size, fill, idx[i], i
                    if cfg.mode == "pct":
                        stop_px = fill * (1 - cfg.pct_sl) if side == 1 else fill * (1 + cfg.pct_sl)
                        tp_px = fill * (1 + cfg.pct_tp) if side == 1 else fill * (1 - cfg.pct_tp)
                    elif side == 1:
                        stop_px = fill - risk_per_unit
                        tp_px = fill + cfg.atr_mult_tp * a
                    else:
                        stop_px = fill + risk_per_unit
                        tp_px = fill - cfg.atr_mult_tp * a

        # --- 2) Durante la vela: comprobar SL/TP (peor caso → stop primero) ---
        if pos == 1:
            if l[i] <= stop_px:
                close_position(stop_px, idx[i], "stop")
            elif cfg.use_tp and h[i] >= tp_px:
                close_position(tp_px, idx[i], "target")
        elif pos == -1:
            if h[i] >= stop_px:
                close_position(stop_px, idx[i], "stop")
            elif cfg.use_tp and l[i] <= tp_px:
                close_position(tp_px, idx[i], "target")

        # --- 2b) Cierre por TIEMPO (scalp): si sigue abierta pasadas N velas ---
        if pos != 0 and cfg.max_hold_bars > 0 and (i - entry_bar) >= cfg.max_hold_bars:
            close_position(c[i], idx[i], "timeout")

        # --- 3) Marcar equity a mercado (cierre de la vela) ---
        equity_curve[i] = equity + ((c[i] - entry_px) * qty * pos if pos != 0 else 0.0)

        # --- 4) Ruina: si el capital realizado se agota, detener ---
        if equity <= 0:
            equity_curve[i:] = 0.0
            pos = 0
            break

    if pos != 0:
        close_position(c[-1], idx[-1], "signal")
        equity_curve[-1] = equity

    eq = pd.Series(equity_curve, index=idx).ffill().fillna(cfg.initial_capital)
    return trades, eq
