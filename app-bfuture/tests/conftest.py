"""Fixtures y dobles de prueba compartidos.

`pythonpath=["src"]` (pyproject) hace que `import binance` funcione sin instalar.
"""
import pytest

from binance.filters import FilterCache, parse_symbol_filters

# exchangeInfo sintético (BTCUSDT recortado, minNotional bajo para facilitar tests).
FAKE_BTC = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "contractType": "PERPETUAL",
    "pricePrecision": 2,
    "quantityPrecision": 3,
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.10", "maxPrice": "1000000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.95"},
    ],
}


@pytest.fixture
def fake_btc():
    return dict(FAKE_BTC)


@pytest.fixture
def sf():
    return parse_symbol_filters(FAKE_BTC)


@pytest.fixture
def filters():
    fc = FilterCache(None)
    fc._symbols = {"BTCUSDT": parse_symbol_filters(FAKE_BTC)}
    return fc


class FakeClient:
    """Doble del cliente: captura las llamadas a _request y responde canned."""

    def __init__(self, responses=None, raise_on=None, mark="60000"):
        self.calls = []
        self.ws_base = "wss://test"
        self._responses = responses or {}
        self._raise_on = raise_on or {}
        self._mark = mark

    async def _request(self, method, path, params=None, *, signed=False):
        self.calls.append({"method": method, "path": path,
                           "params": params or {}, "signed": signed})
        if (method, path) in self._raise_on:
            raise self._raise_on[(method, path)]
        if path in self._responses:
            r = self._responses[path]
            return r(params) if callable(r) else r
        return {"ok": True, "path": path, **(params or {})}

    async def mark_price(self, symbol=None):
        return {"markPrice": self._mark}

    async def position_risk(self, symbol=None):
        # Por defecto simula una posición abierta (para brackets closePosition).
        return [{"symbol": symbol or "BTCUSDT", "positionAmt": "0.002",
                 "entryPrice": self._mark}]

    async def book_ticker(self, symbol):
        m = float(self._mark)
        return {"symbol": symbol, "bidPrice": str(m * 0.9999), "askPrice": str(m * 1.0001)}

    def find(self, method, path):
        return [c for c in self.calls if c["method"] == method and c["path"] == path]


@pytest.fixture
def make_fake_client():
    def _make(**kw):
        return FakeClient(**kw)
    return _make
