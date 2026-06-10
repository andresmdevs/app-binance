"""Capa de acceso a Binance USDⓈ-M Futures (cliente REST + filtros de símbolo).

Submódulos:
    client   -> BinanceFuturesClient: cliente async robusto (firma, rate-limit, errores).
    filters  -> FilterCache / SymbolFilters: validación y cuantización de órdenes.
"""
from .client import (
    BinanceAPIError,
    BinanceError,
    BinanceFuturesClient,
    BinanceRateLimit,
    BinanceServerBusy,
    BinanceUnknownStatus,
)
from .audit import AuditLog
from .filters import FilterCache, FilterError, SymbolFilters
from .risk import RiskError, RiskLimits, RiskManager
from .scalp import (ScalpConfig, bracket_prices, close_scalp,
                    manage_trailing_stop, open_scalp)
from .trade import FuturesTrader, new_client_order_id
from .ws import MarketStream, UserStream

__all__ = [
    "BinanceFuturesClient",
    "BinanceError",
    "BinanceAPIError",
    "BinanceRateLimit",
    "BinanceServerBusy",
    "BinanceUnknownStatus",
    "FilterCache",
    "SymbolFilters",
    "FilterError",
    "FuturesTrader",
    "new_client_order_id",
    "MarketStream",
    "UserStream",
    "RiskManager",
    "RiskLimits",
    "RiskError",
    "AuditLog",
    "ScalpConfig",
    "open_scalp",
    "close_scalp",
    "bracket_prices",
    "manage_trailing_stop",
]
