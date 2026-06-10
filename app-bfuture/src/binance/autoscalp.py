"""Lógica del scalp AUTOMÁTICO: niveles, validación de momentum y trailing stop.

Todo lo de aquí es PURO/determinista (sin red) salvo `recent_return_pct`, para
poder testearlo. La estrategia que pidió el usuario:
  * Objetivo 35% ROE sobre el margen  -> TP (precio) = target_roe / apalancamiento.
  * Cantidad = margen fijo × apalancamiento.
  * Entrada apta si: precio bajo, repunte 24h y fuerza en velas de 1m.
  * Stop que se mueve a break-even y luego trailing al avanzar el precio.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional


def auto_levels(leverage: int, *, target_roe: float = 0.35, stop_roe: float = 0.5,
                max_stop_frac: float = 0.85) -> tuple[float, float]:
    """Devuelve (tp_pct, sl_pct) como fracción de PRECIO a partir del ROE objetivo.

    tp_pct = target_roe / lev ; sl_pct = stop_roe / lev, acotado por debajo de la
    distancia de liquidación (~1/lev) con `max_stop_frac` de margen de seguridad.
    """
    lev = max(1, int(leverage))
    tp = target_roe / lev
    sl = min(stop_roe / lev, max_stop_frac / lev)
    return tp, sl


def auto_notional(margin, leverage) -> Decimal:
    """Notional = margen × apalancamiento (cantidad auto = margen fijo)."""
    return Decimal(str(margin)) * Decimal(str(leverage))


def momentum_ok(*, side: str, last_price: float, change_pct_24h: float,
                recent_return_pct: float, max_price: float,
                min_24h: float = 2.0, min_1m: float = 0.0) -> tuple[bool, str]:
    """¿La moneda es apta para entrar en `side` (BUY/SELL)? Devuelve (ok, motivo)."""
    side = side.upper()
    if last_price <= 0 or last_price > max_price:
        return False, f"precio {last_price:g} fuera de rango (≤ {max_price:g})"
    if side == "BUY":
        if change_pct_24h < min_24h:
            return False, f"cambio 24h {change_pct_24h:+.1f}% sin repunte (≥ {min_24h:g}%)"
        if recent_return_pct <= min_1m:
            return False, f"sin fuerza alcista en 1m ({recent_return_pct:+.2f}%)"
    else:
        if change_pct_24h > -min_24h:
            return False, f"cambio 24h {change_pct_24h:+.1f}% no es bajista (≤ -{min_24h:g}%)"
        if recent_return_pct >= -min_1m:
            return False, f"sin fuerza bajista en 1m ({recent_return_pct:+.2f}%)"
    return True, "apta (precio bajo + repunte 24h + fuerza 1m)"


def next_stop_level(side: str, entry: float, mark: float, current_stop: Optional[float],
                    *, be_trigger_pct: float, trail_pct: float,
                    be_buffer: float = 0.0005) -> Optional[float]:
    """Nuevo nivel de stop si debe moverse a positivo (BE) o trailing; si no, None.

    Cuando el precio supera el break-even por `be_trigger_pct`, el stop sube al
    punto de equilibrio (+buffer de comisiones) y luego va trailando a
    `trail_pct` por detrás del precio — solo en la dirección que protege ganancia.
    """
    side = side.upper()
    if side == "BUY":
        if (mark - entry) / entry < be_trigger_pct:
            return None
        target = max(entry * (1 + be_buffer), mark * (1 - trail_pct))
        if current_stop is None or target > current_stop:
            return target
        return None
    else:  # SELL / short
        if (entry - mark) / entry < be_trigger_pct:
            return None
        target = min(entry * (1 - be_buffer), mark * (1 + trail_pct))
        if current_stop is None or target < current_stop:
            return target
        return None


async def recent_return_pct(client, symbol: str, bars: int = 5) -> float:
    """Retorno % de las últimas `bars` velas de 1m (fuerza reciente)."""
    kl = await client.klines(symbol, "1m", limit=bars + 1)
    if len(kl) < 2:
        return 0.0
    first_open = float(kl[0][1])
    last_close = float(kl[-1][4])
    return (last_close - first_open) / first_open * 100.0 if first_open else 0.0
