"""Guardas de riesgo y kill-switch (USDⓈ-M Futures).

Antes de ABRIR cualquier posición, `RiskManager.check_open` valida la intención
contra el estado real de la cuenta y unos límites configurables. Si algo se viola,
lanza `RiskError` y la orden NO se envía. Cerrar posiciones nunca se bloquea.

La decisión está aislada en `evaluate_open` (función pura, testeable sin red); el
manager solo reúne el estado (posiciones + PnL realizado del día) y la invoca.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Optional


class RiskError(Exception):
    """Una guarda de riesgo bloqueó la operación."""


@dataclass
class RiskLimits:
    max_order_notional: Decimal = Decimal("1000")     # tamaño máx por orden
    max_position_notional: Decimal = Decimal("5000")  # exposición máx por símbolo
    max_open_positions: int = 5                        # nº máx de posiciones a la vez
    max_leverage: int = 10                             # tope de apalancamiento
    daily_loss_limit: Decimal = Decimal("100")         # kill-switch: pérdida diaria


def evaluate_open(
    limits: RiskLimits,
    *,
    symbol: str,
    order_notional: Decimal,
    open_symbols: set,
    symbol_notional: Decimal,
    daily_realized: Decimal,
) -> Optional[str]:
    """Devuelve un mensaje de bloqueo, o None si la apertura es admisible."""
    if order_notional > limits.max_order_notional:
        return (f"Orden de {order_notional:.2f} supera el máximo por orden "
                f"({limits.max_order_notional}).")
    if daily_realized <= -limits.daily_loss_limit:
        return (f"KILL-SWITCH: pérdida diaria {daily_realized:.2f} alcanzó el límite "
                f"(-{limits.daily_loss_limit}). No se abren nuevas posiciones hoy.")
    if symbol not in open_symbols and len(open_symbols) >= limits.max_open_positions:
        return (f"Máx. de posiciones abiertas ({limits.max_open_positions}) alcanzado.")
    if symbol_notional + order_notional > limits.max_position_notional:
        return (f"Exposición en {symbol} ({symbol_notional:.2f}+{order_notional:.2f}) "
                f"superaría el máximo ({limits.max_position_notional}).")
    return None


def _utc_midnight_ms() -> int:
    now = datetime.now(timezone.utc)
    midnight = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    return int(midnight.timestamp() * 1000)


class RiskManager:
    def __init__(self, client, limits: Optional[RiskLimits] = None):
        self.client = client
        self.limits = limits or RiskLimits()

    async def realized_pnl_today(self) -> Decimal:
        rows = await self.client.income(
            incomeType="REALIZED_PNL", startTime=_utc_midnight_ms(), limit=1000)
        return sum((Decimal(str(r.get("income", 0))) for r in rows), Decimal("0"))

    async def check_open(self, symbol: str, order_notional) -> None:
        """Valida una apertura. Lanza RiskError si viola alguna guarda."""
        order_notional = Decimal(str(order_notional))
        positions = await self.client.position_risk()
        open_symbols = set()
        symbol_notional = Decimal("0")
        for p in positions:
            if float(p.get("positionAmt", 0)) == 0:
                continue
            open_symbols.add(p["symbol"])
            if p["symbol"] == symbol:
                symbol_notional = abs(Decimal(str(p.get("notional", 0) or 0)))
        daily = await self.realized_pnl_today()
        msg = evaluate_open(
            self.limits, symbol=symbol, order_notional=order_notional,
            open_symbols=open_symbols, symbol_notional=symbol_notional,
            daily_realized=daily)
        if msg:
            raise RiskError(msg)
