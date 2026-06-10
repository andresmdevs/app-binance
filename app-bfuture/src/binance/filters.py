"""Validación y cuantización de órdenes según los filtros de cada símbolo.

Antes de enviar CUALQUIER orden hay que ajustar precio y cantidad a las reglas
que publica el exchange en ``GET /fapi/v1/exchangeInfo``. Si no, Binance la
rechaza (-1111 precisión, -4014 tick, -4164 notional mínimo, etc.).

Reglas que aplicamos (por símbolo):
    PRICE_FILTER      -> precio múltiplo de ``tickSize`` y dentro de [minPrice, maxPrice]
    LOT_SIZE          -> cantidad LIMIT múltiplo de ``stepSize`` y en [minQty, maxQty]
    MARKET_LOT_SIZE   -> ídem para órdenes MARKET
    MIN_NOTIONAL      -> precio * cantidad >= ``notional`` mínimo
    PERCENT_PRICE     -> precio dentro de la banda permitida respecto al mark price

TODO el cálculo se hace con ``Decimal`` (NUNCA float) para no introducir errores
de redondeo que el exchange rechazaría.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from .client import BinanceError


class FilterError(BinanceError):
    """La orden no cumple un filtro y no es ajustable automáticamente.

    Hereda de BinanceError para que cualquier ``except BinanceError`` de la UI
    lo capture y muestre un aviso amable (en vez de un traceback).
    """


def _d(value) -> Decimal:
    """Convierte a Decimal pasando por str (evita el ruido binario de float)."""
    return Decimal(str(value))


def _round_to_step(value: Decimal, step: Decimal, rounding) -> Decimal:
    """Redondea ``value`` al múltiplo de ``step`` más cercano según ``rounding``.

    Funciona con steps que no son potencias de 10 (p.ej. tickSize = 0.5).
    """
    if step == 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def _decimals_of(step: Decimal) -> int:
    """Nº de decimales que impone un step (para formatear sin notación científica)."""
    exp = step.normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


@dataclass
class SymbolFilters:
    """Reglas de trading de un símbolo, ya parseadas a Decimal."""

    symbol: str
    status: str
    contract_type: str
    price_precision: int
    quantity_precision: int
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    multiplier_up: Optional[Decimal] = None
    multiplier_down: Optional[Decimal] = None

    # --- Formateo a string listo para la API --------------------------------
    def format_price(self, price) -> str:
        q = _round_to_step(_d(price), self.tick_size, ROUND_HALF_UP)
        return f"{q:.{_decimals_of(self.tick_size)}f}"

    def format_qty(self, qty, *, market: bool = False) -> str:
        step = self.market_step_size if market else self.step_size
        # Para cantidad redondeamos SIEMPRE hacia abajo: nunca pasarse del tamaño deseado.
        q = _round_to_step(_d(qty), step, ROUND_DOWN)
        return f"{q:.{_decimals_of(step)}f}"

    # --- Validación + ajuste --------------------------------------------------
    def validate(
        self,
        *,
        side: str,
        quantity,
        price=None,
        order_type: str = "LIMIT",
        mark_price=None,
    ) -> dict:
        """Devuelve ``{"quantity": str, "price": str|None}`` ya ajustados, o lanza
        ``FilterError`` si la orden no es válida ni siquiera tras ajustar.
        """
        is_market = order_type.upper() in ("MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET")
        step = self.market_step_size if is_market else self.step_size
        min_qty = self.market_min_qty if is_market else self.min_qty
        max_qty = self.market_max_qty if is_market else self.max_qty

        try:
            qty = _round_to_step(_d(quantity), step, ROUND_DOWN)
        except (InvalidOperation, ValueError) as e:
            raise FilterError(f"Cantidad inválida: {quantity!r}") from e

        if qty <= 0:
            raise FilterError(f"Cantidad {quantity} se redondea a 0 (stepSize={step}).")
        if qty < min_qty:
            raise FilterError(f"Cantidad {qty} < minQty {min_qty} para {self.symbol}.")
        if max_qty > 0 and qty > max_qty:
            raise FilterError(f"Cantidad {qty} > maxQty {max_qty} para {self.symbol}.")

        out_price: Optional[Decimal] = None
        if not is_market:
            if price is None:
                raise FilterError(f"Orden {order_type} requiere precio.")
            out_price = _round_to_step(_d(price), self.tick_size, ROUND_HALF_UP)
            if out_price < self.min_price or (self.max_price > 0 and out_price > self.max_price):
                raise FilterError(
                    f"Precio {out_price} fuera de [{self.min_price}, {self.max_price}]."
                )

        # MIN_NOTIONAL: para MARKET usamos el mark price como referencia de precio.
        ref_price = out_price if out_price is not None else (
            _d(mark_price) if mark_price is not None else None
        )
        if ref_price is not None and self.min_notional > 0:
            notional = ref_price * qty
            if notional < self.min_notional:
                raise FilterError(
                    f"Notional {notional} < mínimo {self.min_notional} para {self.symbol} "
                    f"(precio {ref_price} x cantidad {qty})."
                )

        # PERCENT_PRICE: precio dentro de banda respecto al mark price.
        if out_price is not None and mark_price is not None and self.multiplier_up:
            mp = _d(mark_price)
            upper = mp * self.multiplier_up
            lower = mp * (self.multiplier_down or Decimal(0))
            if out_price > upper or out_price < lower:
                raise FilterError(
                    f"Precio {out_price} fuera de la banda PERCENT_PRICE "
                    f"[{lower}, {upper}] (mark={mp})."
                )

        return {
            "quantity": f"{qty:.{_decimals_of(step)}f}",
            "price": None if out_price is None else f"{out_price:.{_decimals_of(self.tick_size)}f}",
        }


def parse_symbol_filters(symbol_info: dict) -> SymbolFilters:
    """Construye un ``SymbolFilters`` a partir de una entrada de exchangeInfo['symbols']."""
    f = {flt["filterType"]: flt for flt in symbol_info.get("filters", [])}
    pf = f.get("PRICE_FILTER", {})
    ls = f.get("LOT_SIZE", {})
    mls = f.get("MARKET_LOT_SIZE", ls)
    # En futuros el filtro suele llamarse MIN_NOTIONAL con campo 'notional'.
    mn = f.get("MIN_NOTIONAL", {})
    pp = f.get("PERCENT_PRICE", {})

    return SymbolFilters(
        symbol=symbol_info["symbol"],
        status=symbol_info.get("status", ""),
        contract_type=symbol_info.get("contractType", ""),
        price_precision=int(symbol_info.get("pricePrecision", 0)),
        quantity_precision=int(symbol_info.get("quantityPrecision", 0)),
        tick_size=_d(pf.get("tickSize", "0")),
        min_price=_d(pf.get("minPrice", "0")),
        max_price=_d(pf.get("maxPrice", "0")),
        step_size=_d(ls.get("stepSize", "0")),
        min_qty=_d(ls.get("minQty", "0")),
        max_qty=_d(ls.get("maxQty", "0")),
        market_step_size=_d(mls.get("stepSize", ls.get("stepSize", "0"))),
        market_min_qty=_d(mls.get("minQty", ls.get("minQty", "0"))),
        market_max_qty=_d(mls.get("maxQty", ls.get("maxQty", "0"))),
        min_notional=_d(mn.get("notional", mn.get("minNotional", "0"))),
        multiplier_up=_d(pp["multiplierUp"]) if pp.get("multiplierUp") else None,
        multiplier_down=_d(pp["multiplierDown"]) if pp.get("multiplierDown") else None,
    )


class FilterCache:
    """Cachea exchangeInfo y expone los filtros por símbolo.

    Uso::

        cache = FilterCache(client)
        await cache.refresh()
        adj = cache.validate("BTCUSDT", side="BUY", price=64000.07, quantity=0.0013)
        # -> {"quantity": "0.001", "price": "64000.10"}  (listo para enviar)
    """

    def __init__(self, client, *, only_perpetual: bool = True):
        self._client = client
        self._only_perpetual = only_perpetual
        self._symbols: dict[str, SymbolFilters] = {}

    async def refresh(self) -> None:
        info = await self._client.exchange_info()
        symbols: dict[str, SymbolFilters] = {}
        for s in info.get("symbols", []):
            if self._only_perpetual and s.get("contractType") != "PERPETUAL":
                continue
            symbols[s["symbol"]] = parse_symbol_filters(s)
        self._symbols = symbols

    def get(self, symbol: str) -> SymbolFilters:
        try:
            return self._symbols[symbol]
        except KeyError:
            raise FilterError(
                f"Símbolo {symbol} no está en exchangeInfo (¿refrescaste la cache?)."
            )

    def symbols(self) -> set:
        """Conjunto de símbolos cacheados (p.ej. solo PERPETUAL)."""
        return set(self._symbols)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._symbols

    def __len__(self) -> int:
        return len(self._symbols)

    def validate(self, symbol: str, **kwargs) -> dict:
        return self.get(symbol).validate(**kwargs)
