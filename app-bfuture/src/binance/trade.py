"""Módulo de ejecución de órdenes (USDⓈ-M Futures).

Se apoya en:
  * BinanceFuturesClient (firma, rate-limit, errores, estado desconocido).
  * FilterCache (cuantización de precio/cantidad -> evita rechazos por filtros).

Diseño clave:
  * TODA orden se valida contra los filtros del símbolo ANTES de enviarse.
  * TODA orden lleva un ``newClientOrderId`` propio -> idempotencia y
    reconciliación.
  * Si el envío termina en ESTADO DESCONOCIDO (HTTP 503 / -1007 / -1001 / red),
    NO se reintenta a ciegas: se consulta la orden por su clientOrderId para
    saber si realmente entró, y solo así se decide. Esto evita órdenes duplicadas.

Soporta modo one-way (positionSide="BOTH", con reduceOnly para cerrar) y modo
hedge (positionSide="LONG"/"SHORT").

Endpoints: POST/GET/DELETE /fapi/v1/order (LIMIT/MARKET); POST/GET/DELETE
/fapi/v1/algoOrder + GET /fapi/v1/openAlgoOrders (CONDICIONALES: STOP*/TP*/
TRAILING — migradas al servicio Algo el 2025-12-09; usarlas en /fapi/v1/order
da error -4120); DELETE /fapi/v1/allOpenOrders; POST /fapi/v1/leverage,
/fapi/v1/marginType, /fapi/v1/positionSide/dual.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Optional

from .client import (
    BinanceAPIError,
    BinanceFuturesClient,
    BinanceUnknownStatus,
)
from .filters import FilterCache

# Tipos de orden.
LIMIT = "LIMIT"
MARKET = "MARKET"
STOP = "STOP"                      # stop con precio límite (condicional)
STOP_MARKET = "STOP_MARKET"
TAKE_PROFIT = "TAKE_PROFIT"
TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"

# Desde 2025-12-09 las órdenes CONDICIONALES van por el endpoint Algo
# (POST /fapi/v1/algoOrder); en /fapi/v1/order devuelven -4120.
_CONDITIONAL_TYPES = {STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET,
                      TRAILING_STOP_MARKET}
_ALGO_LIMIT_TYPES = {STOP, TAKE_PROFIT}  # condicionales con precio límite

# Códigos "no hace falta cambiar" -> se tratan como no-op (no es un error real).
CODE_NO_NEED_CHANGE_MARGIN = -4046
CODE_NO_NEED_CHANGE_POSITION_MODE = -4059
CODE_NO_SUCH_ORDER = -2013


def new_client_order_id(prefix: str = "bf") -> str:
    """ID de cliente único y válido (^[\\.A-Za-z0-9_-]{1,36}$)."""
    return f"{prefix}{int(time.time() * 1000)}{secrets.token_hex(3)}"


def _b(value: Optional[bool]) -> Optional[str]:
    """Binance espera booleanos como 'true'/'false' en minúscula."""
    return None if value is None else ("true" if value else "false")


class FuturesTrader:
    def __init__(self, client: BinanceFuturesClient, filters: FilterCache, audit=None):
        self.client = client
        self.filters = filters
        self.audit = audit  # objeto con .record(action, request, response, error) o None

    def _record(self, action, request=None, response=None, error=None):
        if self.audit is not None:
            try:
                self.audit.record(action, request, response, error)
            except Exception:
                pass  # el log nunca debe interrumpir el trading

    # --- Configuración de cuenta/símbolo --------------------------------------
    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self.client._request(
            "POST", "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": int(leverage)}, signed=True,
        )

    async def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        """margin_type: 'ISOLATED' o 'CROSSED'. Idempotente."""
        try:
            return await self.client._request(
                "POST", "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type.upper()}, signed=True,
            )
        except BinanceAPIError as e:
            if e.code == CODE_NO_NEED_CHANGE_MARGIN:
                return {"code": e.code, "msg": "sin cambios (ya estaba así)"}
            raise

    async def set_position_mode(self, hedge: bool) -> dict:
        """hedge=True -> modo cobertura (LONG/SHORT); False -> one-way. Idempotente."""
        try:
            return await self.client._request(
                "POST", "/fapi/v1/positionSide/dual",
                {"dualSidePosition": _b(hedge)}, signed=True,
            )
        except BinanceAPIError as e:
            if e.code == CODE_NO_NEED_CHANGE_POSITION_MODE:
                return {"code": e.code, "msg": "sin cambios (ya estaba así)"}
            raise

    async def get_position_mode(self) -> bool:
        data = await self.client._request(
            "GET", "/fapi/v1/positionSide/dual", signed=True
        )
        return bool(data.get("dualSidePosition"))

    # --- Consulta y cancelación ----------------------------------------------
    async def query_order(
        self, symbol: str, *, order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        if order_id is None and client_order_id is None:
            raise ValueError("Se requiere order_id u client_order_id.")
        return await self.client._request(
            "GET", "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
            signed=True,
        )

    async def cancel_order(
        self, symbol: str, *, order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        if order_id is None and client_order_id is None:
            raise ValueError("Se requiere order_id u client_order_id.")
        req = {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id}
        self._record("cancel", req)
        try:
            res = await self.client._request("DELETE", "/fapi/v1/order", req, signed=True)
            self._record("cancel.ok", req, res)
            return res
        except Exception as e:
            self._record("cancel.error", req, error=e)
            raise

    async def cancel_all(self, symbol: str) -> dict:
        return await self.client._request(
            "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True
        )

    # --- Colocación de órdenes LIMIT/MARKET (validación + reconciliación) ------
    async def place_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity=None,
        price=None,
        stop_price=None,
        position_side: str = "BOTH",
        time_in_force: str = "GTC",
        reduce_only: Optional[bool] = None,
        close_position: Optional[bool] = None,
        working_type: Optional[str] = None,
        client_order_id: Optional[str] = None,
        mark_price=None,
    ) -> dict:
        order_type = order_type.upper()
        side = side.upper()
        # Las condicionales (STOP*/TAKE_PROFIT*/TRAILING) van por el endpoint Algo.
        if order_type in _CONDITIONAL_TYPES:
            return await self.place_algo_order(
                symbol=symbol, side=side, order_type=order_type, quantity=quantity,
                price=price, trigger_price=stop_price, position_side=position_side,
                time_in_force=time_in_force, reduce_only=reduce_only,
                close_position=close_position, working_type=working_type,
                client_algo_id=client_order_id, mark_price=mark_price)

        sf = self.filters.get(symbol)
        if quantity is None:
            raise ValueError(f"{order_type} requiere quantity.")
        if order_type == MARKET and mark_price is None:
            mp = await self.client.mark_price(symbol)
            mark_price = (mp[0] if isinstance(mp, list) else mp).get("markPrice")
        adj = sf.validate(side=side, quantity=quantity, price=price,
                          order_type=order_type, mark_price=mark_price)
        coid = client_order_id or new_client_order_id()
        params = {
            "symbol": symbol, "side": side, "type": order_type,
            "positionSide": position_side, "newClientOrderId": coid,
            "quantity": adj["quantity"], "workingType": working_type,
        }
        if order_type == LIMIT:
            params["price"] = adj["price"]
            params["timeInForce"] = time_in_force
        if position_side == "BOTH" and reduce_only is not None:
            params["reduceOnly"] = _b(reduce_only)

        self._record("order", params)
        try:
            resp = await self.client._request("POST", "/fapi/v1/order", params, signed=True)
            self._record("order.ok", params, resp)
            return resp
        except BinanceUnknownStatus as ue:
            self._record("order.unknown", params, error=ue)
            await asyncio.sleep(1.0)
            try:
                order = await self.query_order(symbol, client_order_id=coid)
                self._record("order.reconciled", params, order)
                return order
            except BinanceAPIError as e:
                if e.code == CODE_NO_SUCH_ORDER:
                    self._record("order.not_created", params, error=e)
                    raise BinanceUnknownStatus(
                        f"La orden {coid} no se creó; seguro reintentar con el mismo id."
                    ) from e
                raise
        except Exception as e:
            self._record("order.error", params, error=e)
            raise

    # --- Colocación de órdenes CONDICIONALES (endpoint Algo) ------------------
    async def place_algo_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity=None,
        price=None,
        trigger_price=None,
        position_side: str = "BOTH",
        time_in_force: str = "GTC",
        reduce_only: Optional[bool] = None,
        close_position: Optional[bool] = None,
        working_type: Optional[str] = None,
        activation_price=None,
        callback_rate=None,
        client_algo_id: Optional[str] = None,
        mark_price=None,
    ) -> dict:
        order_type = order_type.upper()
        side = side.upper()
        sf = self.filters.get(symbol)
        is_trailing = order_type == TRAILING_STOP_MARKET

        adj_qty: Optional[str] = None
        adj_price: Optional[str] = None
        if not close_position:
            if quantity is None:
                raise ValueError(f"{order_type} requiere quantity (o close_position).")
            if mark_price is None:
                mp = await self.client.mark_price(symbol)
                mark_price = (mp[0] if isinstance(mp, list) else mp).get("markPrice")
            adj = sf.validate(side=side, quantity=quantity, price=price,
                              order_type=order_type, mark_price=mark_price)
            adj_qty, adj_price = adj["quantity"], adj["price"]

        adj_trigger: Optional[str] = None
        if not is_trailing:
            if trigger_price is None:
                raise ValueError(f"{order_type} requiere trigger_price.")
            adj_trigger = sf.format_price(trigger_price)

        caid = client_algo_id or new_client_order_id()
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol, "side": side, "type": order_type,
            "positionSide": position_side, "clientAlgoId": caid,
            "quantity": adj_qty, "triggerPrice": adj_trigger,
            "workingType": working_type,
        }
        if order_type in _ALGO_LIMIT_TYPES:
            params["price"] = adj_price
            params["timeInForce"] = time_in_force
        if is_trailing:
            if callback_rate is not None:
                params["callbackRate"] = callback_rate
            if activation_price is not None:
                params["activatePrice"] = sf.format_price(activation_price)
        if close_position:
            params["closePosition"] = _b(True)  # incompatible con quantity/reduceOnly
        elif position_side == "BOTH" and reduce_only is not None:
            params["reduceOnly"] = _b(reduce_only)

        self._record("algo", params)
        try:
            resp = await self.client._request("POST", "/fapi/v1/algoOrder", params, signed=True)
            self._record("algo.ok", params, resp)
            return resp
        except BinanceUnknownStatus as ue:
            self._record("algo.unknown", params, error=ue)
            await asyncio.sleep(1.0)
            try:
                order = await self.query_algo_order(client_algo_id=caid)
                self._record("algo.reconciled", params, order)
                return order
            except BinanceError as e:
                self._record("algo.not_confirmed", params, error=e)
                raise BinanceUnknownStatus(
                    f"Algo {caid}: estado desconocido tras timeout."
                ) from e
        except Exception as e:
            self._record("algo.error", params, error=e)
            raise

    async def query_algo_order(self, *, algo_id=None, client_algo_id=None) -> dict:
        if algo_id is None and client_algo_id is None:
            raise ValueError("Se requiere algo_id o client_algo_id.")
        return await self.client._request(
            "GET", "/fapi/v1/algoOrder",
            {"algoId": algo_id, "clientAlgoId": client_algo_id}, signed=True)

    async def cancel_algo_order(self, *, algo_id=None, client_algo_id=None) -> dict:
        if algo_id is None and client_algo_id is None:
            raise ValueError("Se requiere algo_id o client_algo_id.")
        req = {"algoId": algo_id, "clientAlgoId": client_algo_id}
        self._record("algo.cancel", req)
        try:
            res = await self.client._request("DELETE", "/fapi/v1/algoOrder", req, signed=True)
            self._record("algo.cancel.ok", req, res)
            return res
        except Exception as e:
            self._record("algo.cancel.error", req, error=e)
            raise

    async def open_algo_orders(self, symbol: Optional[str] = None) -> list:
        return await self.client._request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)

    # --- Atajos --------------------------------------------------------------
    async def market(self, symbol: str, side: str, quantity, **kw) -> dict:
        return await self.place_order(
            symbol=symbol, side=side, order_type=MARKET, quantity=quantity, **kw
        )

    async def limit(self, symbol: str, side: str, quantity, price, **kw) -> dict:
        return await self.place_order(
            symbol=symbol, side=side, order_type=LIMIT,
            quantity=quantity, price=price, **kw
        )

    async def stop_market(
        self, symbol: str, side: str, stop_price, quantity=None,
        close_position: bool = False, **kw,
    ) -> dict:
        return await self.place_algo_order(
            symbol=symbol, side=side, order_type=STOP_MARKET,
            trigger_price=stop_price, quantity=quantity,
            close_position=close_position, **kw,
        )

    async def close_market(
        self, symbol: str, side: str, quantity, position_side: str = "BOTH", **kw,
    ) -> dict:
        """Cierra (parcial o total) con MARKET. ``side`` es el lado de CIERRE
        (opuesto a la posición). En one-way añade reduceOnly automáticamente.
        """
        reduce_only = True if position_side == "BOTH" else None
        return await self.place_order(
            symbol=symbol, side=side, order_type=MARKET, quantity=quantity,
            position_side=position_side, reduce_only=reduce_only, **kw,
        )

    # --- Stop-loss de protección ---------------------------------------------
    async def place_stop_loss(
        self, symbol: str, *, entry_side: str, stop_price, position_side: str = "BOTH",
    ) -> dict:
        """STOP_MARKET que CIERRA toda la posición al tocar ``stop_price``.
        ``entry_side`` es el lado de la ENTRADA (BUY para long / SELL para short).
        Dispara sobre MARK_PRICE.
        """
        stop_side = "SELL" if entry_side.upper() == "BUY" else "BUY"
        return await self.stop_market(
            symbol, stop_side, stop_price, close_position=True,
            position_side=position_side, working_type="MARK_PRICE")

    async def open_with_protection(
        self, symbol: str, side: str, quantity, *, stop_pct, mark_price,
        position_side: str = "BOTH",
    ) -> dict:
        """Abre MARKET y coloca de inmediato un stop-loss de protección a
        ``stop_pct`` (fracción, p.ej. 0.02 = 2%) del precio de marca.

        Devuelve {"entry": <orden>, "stop": <orden|None>, "stop_error": <str|None>}.
        Si el stop falla, la entrada YA está abierta: se informa para que el
        usuario actúe (no se deja la posición desprotegida en silencio).
        """
        from decimal import Decimal

        entry = await self.market(symbol, side, quantity,
                                  position_side=position_side, mark_price=mark_price)
        mark = Decimal(str(mark_price))
        frac = Decimal(str(stop_pct))
        if side.upper() == "BUY":
            stop_price = mark * (Decimal(1) - frac)
        else:
            stop_price = mark * (Decimal(1) + frac)
        try:
            stop = await self.place_stop_loss(
                symbol, entry_side=side, stop_price=stop_price, position_side=position_side)
            return {"entry": entry, "stop": stop, "stop_error": None}
        except Exception as e:
            return {"entry": entry, "stop": None, "stop_error": str(e)}
