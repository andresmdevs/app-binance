"""Tests de la lógica de UI (autocompletado y sliders), sin lanzar la GUI.

Usamos un `StubPage` con métodos no-op para poder ejecutar los handlers que
llaman a page.update().
"""
import main
from binance import BinanceFuturesClient
from binance.filters import parse_symbol_filters

FAKE_BTC = {
    "symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL",
    "pricePrecision": 2, "quantityPrecision": 3,
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.1", "maxPrice": "1e6"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ],
}


class StubPage:
    def update(self): pass
    def open(self, *a, **k): pass
    def run_task(self, *a, **k): pass


def _app():
    return main.TradingApp(StubPage(), BinanceFuturesClient("k", "s", testnet=True))


def test_symbol_autocomplete_filters():
    app = _app()
    app._all_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "PEPEUSDT"]
    app.symbol_field.value = "SOL"
    app._build_symbol_suggestions()
    assert [b.data for b in app.symbol_suggestions.controls] == ["SOLUSDT"]
    assert app.symbol_suggestions.visible is True


def test_symbol_autocomplete_hidden_on_exact_match():
    app = _app()
    app._all_symbols = ["SOLUSDT", "BTCUSDT"]
    app.symbol_field.value = "SOLUSDT"
    app._build_symbol_suggestions()
    assert app.symbol_suggestions.visible is False


def test_access_key_ok(monkeypatch):
    monkeypatch.setattr(main, "ACCESS_KEY", None)
    assert main.access_key_ok("") is True            # sin clave -> uso local libre
    monkeypatch.setattr(main, "ACCESS_KEY", "s3cret")
    assert main.access_key_ok("s3cret") is True
    assert main.access_key_ok("  s3cret  ") is True  # tolera espacios
    assert main.access_key_ok("nope") is False
    assert main.access_key_ok("") is False


def test_notional_range_uses_symbol_min_and_clamps():
    app = _app()
    app.filters._symbols = {"BTCUSDT": parse_symbol_filters(FAKE_BTC)}
    app._all_symbols = ["BTCUSDT"]
    app._available_balance = 1000.0
    app._leverage = 10
    app.symbol_field.value = "BTCUSDT"
    app._recompute_notional_range()
    assert app.notional_slider.min == 5.0           # mínimo del símbolo
    assert app.notional_slider.max == 5000.0        # cap de riesgo (1000×10 limitado a 5000)
    app._set_notional(10_000_000)                    # por encima del máximo
    assert float(app.notional_field.value) == app.notional_slider.max
    app._set_notional(0)                             # por debajo del mínimo
    assert float(app.notional_field.value) == app.notional_slider.min
