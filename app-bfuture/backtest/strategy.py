"""Estrategias de ejemplo.

Cada estrategia produce una columna 'signal' en {-1, 0, +1} que representa la
POSICIÓN DESEADA (short / plano / long), calculada SIN mirar al futuro: solo usa
información disponible al CIERRE de cada vela. El motor ejecuta esa señal en la
APERTURA de la vela siguiente, de modo que nunca hay lookahead.

Son PUNTOS DE PARTIDA para experimentar, no estrategias garantizadas. El trabajo
del backtester es decirte cuáles aguantan sobre datos reales y cuáles no.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


class Strategy:
    name = "base"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Devuelve una copia de df con, al menos, columnas 'signal' y 'atr'."""
        raise NotImplementedError


@dataclass
class EmaCrossATR(Strategy):
    """Seguimiento de tendencia: long si EMA rápida > EMA lenta, short si <.
    Stops y objetivos los pone el motor en múltiplos de ATR.
    """
    fast: int = 20
    slow: int = 50
    atr_n: int = 14
    name: str = "ema_atr"

    def prepare(self, df):
        out = df.copy()
        out["ema_fast"] = ind.ema(out["close"], self.fast)
        out["ema_slow"] = ind.ema(out["close"], self.slow)
        out["atr"] = ind.atr(out, self.atr_n)
        sig = np.where(out["ema_fast"] > out["ema_slow"], 1.0,
                       np.where(out["ema_fast"] < out["ema_slow"], -1.0, 0.0))
        out["signal"] = pd.Series(sig, index=out.index)
        out.loc[out["ema_slow"].isna() | out["atr"].isna(), "signal"] = 0.0
        return out


@dataclass
class RsiReversion(Strategy):
    """Reversión a la media: long en sobreventa, short en sobrecompra,
    se mantiene hasta que el RSI cruza el nivel medio.
    """
    rsi_n: int = 14
    low: float = 30.0
    high: float = 70.0
    exit_level: float = 50.0
    atr_n: int = 14
    name: str = "rsi_rev"

    def prepare(self, df):
        out = df.copy()
        r = ind.rsi(out["close"], self.rsi_n)
        out["rsi"] = r
        out["atr"] = ind.atr(out, self.atr_n)

        pos = np.zeros(len(out))
        cur = 0
        rv = r.values
        for i in range(len(out)):
            x = rv[i]
            if cur == 0:
                if x < self.low:
                    cur = 1
                elif x > self.high:
                    cur = -1
            elif cur == 1 and x >= self.exit_level:
                cur = 0
            elif cur == -1 and x <= self.exit_level:
                cur = 0
            pos[i] = cur
        out["signal"] = pd.Series(pos, index=out.index)
        out.loc[out["atr"].isna(), "signal"] = 0.0
        return out


@dataclass
class DonchianBreakout(Strategy):
    """Ruptura de canal: long al romper el máximo de N velas, short al romper el mínimo."""
    n: int = 20
    atr_n: int = 14
    name: str = "donchian"

    def prepare(self, df):
        out = df.copy()
        upper, lower = ind.donchian(out, self.n)
        out["dc_up"] = upper.shift(1)   # canal de la vela ANTERIOR (sin lookahead)
        out["dc_lo"] = lower.shift(1)
        out["atr"] = ind.atr(out, self.atr_n)
        sig = np.where(out["close"] > out["dc_up"], 1.0,
                       np.where(out["close"] < out["dc_lo"], -1.0, np.nan))
        out["signal"] = pd.Series(sig, index=out.index).ffill().fillna(0.0)
        out.loc[out["atr"].isna(), "signal"] = 0.0
        return out


STRATEGIES = {
    "ema_atr": EmaCrossATR,
    "rsi_rev": RsiReversion,
    "donchian": DonchianBreakout,
}
