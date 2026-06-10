"""Motor de scalping con bracket (TP/SL) y cierre por tiempo.

Ataca el problema que reveló el análisis de producción: las entradas son ~OK, pero
las perdedoras se dejaban correr. Aquí cada scalp nace con:
  * Take-profit fijo (TAKE_PROFIT_MARKET closePosition)
  * Stop-loss fijo (STOP_MARKET closePosition)
  * Cierre por TIEMPO máximo (lo gestiona la capa de UI con un temporizador)

TP y SL se colocan como órdenes CONDICIONALES (endpoint Algo) con closePosition=true,
de modo que al dispararse una, la posición se cierra y la otra se auto-cancela (OCO).
Disparan sobre MARK_PRICE.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

from .autoscalp import next_stop_level
from .client import BinanceAPIError
from .trade import STOP_MARKET, TAKE_PROFIT_MARKET, FuturesTrader, new_client_order_id

# El servicio Algo a veces va por detrás de positionRisk tras el fill MARKET:
# placear el bracket demasiado rápido devuelve -4509 ("position not available").
CODE_POSITION_NOT_AVAILABLE = -4509


@dataclass
class ScalpConfig:
    notional: Decimal          # tamaño de posición (capital × apalancamiento)
    take_profit_pct: float     # fracción, p.ej. 0.01 = +1%
    stop_loss_pct: float       # fracción, p.ej. 0.005 = -0.5%
    max_hold_seconds: int = 300
    leverage: int = 10
    entry_type: str = "MARKET"     # "MARKET" (taker) | "MAKER" (LIMIT GTX = post-only)
    entry_timeout_s: int = 30      # tiempo de espera del fill maker antes de cancelar


async def _wait_position(client, symbol: str, *, attempts: int = 8, delay: float = 0.4) -> bool:
    """Espera a que la posición esté registrada. Los brackets closePosition exigen
    una posición abierta (si no -> error -4509), y el fill MARKET puede tardar un
    instante en reflejarse.
    """
    for _ in range(attempts):
        positions = await client.position_risk(symbol)
        if any(float(p.get("positionAmt", 0)) != 0 for p in positions):
            return True
        await asyncio.sleep(delay)
    return False


async def _place_bracket(trader: FuturesTrader, *, retries: int = 5, **kw):
    """Coloca una orden condicional, reintentando ante -4509 (sync del servicio Algo)."""
    last = None
    for _ in range(retries):
        try:
            return await trader.place_algo_order(**kw)
        except BinanceAPIError as e:
            last = e
            if e.code == CODE_POSITION_NOT_AVAILABLE:
                await asyncio.sleep(0.7)
                continue
            raise
    raise last


async def _maker_entry(trader, client, sf, symbol, side, qty, config):
    """Entrada LIMIT post-only (GTX = solo maker) al mejor bid/ask; espera el fill
    hasta ``entry_timeout_s`` y, si no llena, cancela. Devuelve (orden, filled)."""
    bt = await client.book_ticker(symbol)
    price = bt["bidPrice"] if side == "BUY" else bt["askPrice"]
    coid = new_client_order_id("bfm")
    entry = await trader.limit(symbol, side, qty, price,
                               time_in_force="GTX", client_order_id=coid)
    filled = False
    for _ in range(max(1, int(config.entry_timeout_s))):
        await asyncio.sleep(1.0)
        positions = await client.position_risk(symbol)
        if any(float(p.get("positionAmt", 0)) != 0 for p in positions):
            filled = True
            break
    try:  # cancelar remanente; si llenó del todo da -2011 y se ignora
        await trader.cancel_order(symbol, client_order_id=coid)
    except Exception:  # noqa: BLE001
        pass
    return entry, filled


def bracket_prices(side: str, entry, take_profit_pct, stop_loss_pct):
    """Devuelve (trigger_take_profit, trigger_stop_loss) según el lado de ENTRADA."""
    entry = Decimal(str(entry))
    tp = Decimal(str(take_profit_pct))
    sl = Decimal(str(stop_loss_pct))
    if side.upper() == "BUY":  # long: TP arriba, SL abajo
        return entry * (Decimal(1) + tp), entry * (Decimal(1) - sl)
    return entry * (Decimal(1) - tp), entry * (Decimal(1) + sl)  # short: invertido


async def open_scalp(
    trader: FuturesTrader, client, *, symbol: str, side: str, config: ScalpConfig,
    risk=None, mark_price=None,
) -> dict:
    """Abre la entrada MARKET y coloca el bracket TP+SL. Devuelve un resumen.

    Si TP o SL fallan, la entrada YA está abierta -> se reporta en ``errors`` para
    que la capa superior actúe (y el cierre por tiempo sigue siendo red de seguridad).
    """
    side = side.upper()
    close_side = "SELL" if side == "BUY" else "BUY"
    sf = trader.filters.get(symbol)

    if mark_price is None:
        mp = await client.mark_price(symbol)
        mark = Decimal(str((mp[0] if isinstance(mp, list) else mp)["markPrice"]))
    else:
        mark = Decimal(str(mark_price))

    if risk is not None:
        await risk.check_open(symbol, config.notional)

    is_market = config.entry_type.upper() != "MAKER"
    qty = sf.format_qty(config.notional / mark, market=is_market)
    await trader.set_leverage(symbol, config.leverage)

    if is_market:
        entry = await trader.market(symbol, side, qty, mark_price=mark)
        await _wait_position(client, symbol)  # el bracket closePosition exige posición
    else:
        entry, filled = await _maker_entry(trader, client, sf, symbol, side, qty, config)
        if not filled:  # no se llenó la entrada límite -> ya cancelada, nada que blindar
            return {"entry": entry, "tp": None, "sl": None, "qty": qty, "mark": mark,
                    "tp_trigger": None, "sl_trigger": None, "errors": {}, "filled": False}

    tp_trigger, sl_trigger = bracket_prices(
        side, mark, config.take_profit_pct, config.stop_loss_pct)

    errors: dict[str, str] = {}
    tp = sl = None
    try:
        tp = await _place_bracket(
            trader, symbol=symbol, side=close_side, order_type=TAKE_PROFIT_MARKET,
            trigger_price=tp_trigger, close_position=True, working_type="MARK_PRICE")
    except Exception as e:  # noqa: BLE001 - se reporta, no se traga el riesgo
        errors["tp"] = str(e)
    try:
        sl = await _place_bracket(
            trader, symbol=symbol, side=close_side, order_type=STOP_MARKET,
            trigger_price=sl_trigger, close_position=True, working_type="MARK_PRICE")
    except Exception as e:  # noqa: BLE001
        errors["sl"] = str(e)

    return {
        "entry": entry, "tp": tp, "sl": sl, "qty": qty, "mark": mark,
        "tp_trigger": tp_trigger, "sl_trigger": sl_trigger, "errors": errors,
        "filled": True,
    }


async def manage_trailing_stop(
    trader: FuturesTrader, client, *, symbol: str, side: str, entry: float,
    initial_stop, sl_algo_id, be_trigger_pct: float, trail_pct: float,
    max_hold_seconds: int, poll_seconds: int = 3,
) -> None:
    """Mueve el stop a break-even y luego lo va trailando mientras el precio avanza.
    Al expirar `max_hold_seconds`, cierra por tiempo. Si la posición ya se cerró
    (TP/SL/manual), termina (el bracket restante se auto-cancela).
    """
    side = side.upper()
    close_side = "SELL" if side == "BUY" else "BUY"
    sf = trader.filters.get(symbol)
    current_stop = float(initial_stop) if initial_stop is not None else None
    current_id = sl_algo_id
    deadline = time.time() + max_hold_seconds

    while time.time() < deadline:
        await asyncio.sleep(poll_seconds)
        positions = [p for p in await client.position_risk(symbol)
                     if float(p.get("positionAmt", 0)) != 0]
        if not positions:
            return
        mp = await client.mark_price(symbol)
        mark = float((mp[0] if isinstance(mp, list) else mp)["markPrice"])
        new = next_stop_level(side, entry, mark, current_stop,
                              be_trigger_pct=be_trigger_pct, trail_pct=trail_pct)
        if new is None:
            continue
        new_price = float(sf.format_price(new))
        if current_stop is not None and new_price == current_stop:
            continue
        try:
            if current_id is not None:
                await trader.cancel_algo_order(algo_id=current_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            r = await trader.place_algo_order(
                symbol=symbol, side=close_side, order_type=STOP_MARKET,
                trigger_price=new_price, close_position=True, working_type="MARK_PRICE")
            current_id = r.get("algoId")
            current_stop = new_price
        except Exception:  # noqa: BLE001
            pass

    await close_scalp(trader, client, symbol)


async def close_scalp(trader: FuturesTrader, client, symbol: str) -> bool:
    """Cierra a mercado cualquier posición del símbolo y cancela sus brackets.
    Devuelve True si había una posición que cerrar (uso del cierre por tiempo).
    """
    positions = [p for p in await client.position_risk(symbol)
                 if float(p.get("positionAmt", 0)) != 0]
    closed = False
    for p in positions:
        amt = float(p["positionAmt"])
        await trader.close_market(symbol, "SELL" if amt > 0 else "BUY", abs(amt))
        closed = True
    for o in await trader.open_algo_orders(symbol):
        try:
            await trader.cancel_algo_order(algo_id=o.get("algoId"))
        except Exception:  # noqa: BLE001
            pass
    return closed
