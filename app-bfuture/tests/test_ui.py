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


# --- Pestaña Auto (motor autónomo) --------------------------------------------
import pytest  # noqa: E402

from binance.engine import AutoEngine  # noqa: E402


def test_auto_tab_present_and_config_parses():
    app = _app()
    assert "Auto" in [t.text for t in app.tabs.tabs]
    app.auto_symbols.value = "sol, doge;BTCUSDT"
    app.auto_margin.value = "3"
    app.auto_lev.value = "4"
    app.auto_min_score.value = "3"
    app.auto_maker.value = True
    cfg = app._auto_config()
    assert cfg.mode == "candle" and cfg.symbols == ["SOLUSDT", "DOGEUSDT", "BTCUSDT"]
    assert str(cfg.margin_usdt) == "3" and cfg.leverage == 4 and cfg.lead_s == 4.0
    assert cfg.signal.min_score == 3 and cfg.entry_type == "MAKER"
    assert cfg.signal.fee_rt == pytest.approx(0.0007)
    app.auto_mode.value = "radar"
    assert app._auto_config().mode == "radar"


def test_auto_config_rejects_bad_numbers():
    app = _app()
    app.auto_margin.value = "abc"
    with pytest.raises(ValueError):
        app._auto_config()


def test_auto_feed_and_stats_render():
    app = _app()
    app._engine = AutoEngine(app.client, app.trader, app.risk)
    app._engine.stats.record(0.25, 0.01)
    app._on_engine_event({"ts": 1_700_000_000_000, "kind": "close",
                          "msg": "BTCUSDT LONG cerrada", "net": 0.25})
    assert len(app.auto_feed.controls) == 1
    assert "ganadas 1" in app.auto_stats.value
