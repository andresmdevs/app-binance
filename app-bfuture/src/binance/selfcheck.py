"""Auto-verificación de la capa Binance (fundamentos).

Ejecuta dos bloques:

  1. OFFLINE (siempre): valida la cuantización de filtros con datos sintéticos.
     No necesita claves ni red. Si algo está mal, lanza AssertionError.

  2. ONLINE (best-effort): si hay red, hace ping/time/exchangeInfo en TESTNET y
     muestra los headers de rate-limit. Si además hay claves reales en .env,
     prueba un endpoint firmado de SOLO LECTURA (positionRisk v3). NUNCA envía
     órdenes.

Uso (desde la carpeta del proyecto):

    PYTHONPATH=src uv run python -m binance.selfcheck
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

from .client import BinanceError, BinanceFuturesClient
from .filters import FilterCache, FilterError, parse_symbol_filters
from .analysis import reconstruct_trades, summarize_trades
from .audit import AuditLog, read_audit
from .market import parse_tickers, summarize_market
from .pnl import filter_events, income_rows_to_events, summarize
from .risk import RiskLimits, evaluate_open

# exchangeInfo sintético (recorta los campos reales de BTCUSDT).
_FAKE_SYMBOL = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "contractType": "PERPETUAL",
    "pricePrecision": 2,
    "quantityPrecision": 3,
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "556.80", "maxPrice": "4529764"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
        {"filterType": "MIN_NOTIONAL", "notional": "100"},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.95"},
    ],
}


def _expect_filter_error(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except FilterError:
        return
    raise AssertionError(f"Se esperaba FilterError en {fn.__name__}({args}, {kwargs})")


def check_offline() -> None:
    print("== OFFLINE: cuantización de filtros ==")
    sf = parse_symbol_filters(_FAKE_SYMBOL)

    # Redondeo de precio a tickSize (0.10) y cantidad a stepSize (0.001).
    assert sf.format_price(64000.07) == "64000.1", sf.format_price(64000.07)
    assert sf.format_qty(0.0013) == "0.001", sf.format_qty(0.0013)
    print("  precio 64000.07 -> 64000.1 (tick 0.10)            OK")
    print("  cantidad 0.0013 -> 0.001 (step 0.001, floor)      OK")

    # Caso válido completo: ajusta precio/cantidad y respeta notional y banda.
    adj = sf.validate(side="BUY", quantity=0.002, price=64000.07, mark_price=64000)
    assert adj == {"quantity": "0.002", "price": "64000.1"}, adj
    print(f"  validate LIMIT válida -> {adj}        OK")

    # Caso MARKET: usa mark price para el notional, sin precio.
    adj_m = sf.validate(side="SELL", quantity=0.0023, order_type="MARKET", mark_price=64000)
    assert adj_m == {"quantity": "0.002", "price": None}, adj_m
    print(f"  validate MARKET -> {adj_m}      OK")

    # Rechazos esperados:
    _expect_filter_error(sf.validate, side="BUY", quantity=0.001, price=64000.07)  # notional < 100
    _expect_filter_error(sf.validate, side="BUY", quantity=0.0004, price=64000.07)  # qty -> 0
    _expect_filter_error(  # fuera de banda PERCENT_PRICE
        sf.validate, side="BUY", quantity=0.002, price=70000, mark_price=64000
    )
    print("  rechazos (notional / qty=0 / banda precio)        OK")

    # FilterCache filtra por PERPETUAL y resuelve símbolos.
    assert sf.contract_type == "PERPETUAL"
    print("  parse PERPETUAL                                   OK")

    # Firma HMAC determinista (vector conocido del cliente).
    cli = BinanceFuturesClient("k", "secret", testnet=True)
    signed = cli._sign({"symbol": "BTCUSDT", "side": "BUY"})
    sig = signed.split("signature=")[1]
    assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig), sig
    assert cli.recv_window == 5000
    print("  firma HMAC-SHA256 (64 hex) + recvWindow=5000      OK")

    # PnL: normalización, filtros y resumen (puros).
    rows = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "12.5", "time": 1700000000000, "tradeId": 1},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-4.0", "time": 1700000100000, "tradeId": 2},
        {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "3.0", "time": 1700000200000, "tradeId": 3},
        {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.5", "time": 1700000050000},
    ]
    ev = income_rows_to_events(rows)
    assert len(ev) == 3, ev  # COMMISSION se ignora
    s = summarize(ev)
    assert (s.count, s.wins, s.losses) == (3, 2, 1), s
    assert abs(s.total - 11.5) < 1e-9 and abs(s.profit_factor - 3.875) < 1e-9, s
    assert len(filter_events(ev, only_positive=True)) == 2
    assert len(filter_events(ev, symbol="BTCUSDT")) == 2
    assert len(filter_events(ev, min_pnl=5)) == 1
    print("  PnL: normalización + filtros + profit_factor      OK")

    # Mercado: parseo + resumen (breadth, volumen, top/loser).
    trows = [
        {"symbol": "BTCUSDT", "lastPrice": "60000", "priceChangePercent": "2.5", "quoteVolume": "1000"},
        {"symbol": "ETHUSDT", "lastPrice": "3000", "priceChangePercent": "-1.0", "quoteVolume": "800"},
        {"symbol": "XRPUSDT", "lastPrice": "0.5", "priceChangePercent": "5.0", "quoteVolume": "200"},
        {"symbol": "NOPEUSDT", "lastPrice": "1", "priceChangePercent": "9.0", "quoteVolume": "9"},  # filtrado
    ]
    tk = parse_tickers(trows, allowed={"BTCUSDT", "ETHUSDT", "XRPUSDT"})
    assert len(tk) == 3
    ms = summarize_market(tk)
    assert (ms.advancers, ms.decliners) == (2, 1), ms
    assert ms.top_gainer.symbol == "XRPUSDT" and ms.top_loser.symbol == "ETHUSDT", ms
    assert abs(ms.total_quote_volume - 2000) < 1e-9, ms
    print("  Mercado: parseo + breadth + top/loser             OK")

    # Riesgo: guardas y kill-switch (función pura).
    lim = RiskLimits(max_order_notional=Decimal("1000"), max_position_notional=Decimal("5000"),
                     max_open_positions=2, max_leverage=10, daily_loss_limit=Decimal("100"))
    base = dict(symbol="BTCUSDT", open_symbols=set(), symbol_notional=Decimal("0"),
                daily_realized=Decimal("0"))
    assert evaluate_open(lim, order_notional=Decimal("100"), **base) is None
    assert evaluate_open(lim, order_notional=Decimal("2000"), **base) is not None      # > por orden
    assert evaluate_open(lim, order_notional=Decimal("100"),
                         **{**base, "daily_realized": Decimal("-100")}) is not None     # kill-switch
    assert evaluate_open(lim, order_notional=Decimal("100"),
                         **{**base, "symbol": "X", "open_symbols": {"A", "B"}}) is not None  # máx pos
    assert evaluate_open(lim, order_notional=Decimal("100"),
                         **{**base, "symbol_notional": Decimal("4950")}) is not None    # exposición
    print("  Riesgo: orden/kill-switch/máx-pos/exposición       OK")

    # Auditoría: escribir y releer una línea.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        log = AuditLog(Path(d) / "orders.jsonl")
        log.record("order", {"symbol": "BTCUSDT"}, {"orderId": 1})
        rows = read_audit(Path(d) / "orders.jsonl")
        assert len(rows) == 1 and rows[0]["action"] == "order", rows
    print("  Auditoría: escribe/relee JSONL                     OK")

    # Análisis: reconstrucción de operaciones (ganadora + perdedora) y resumen.
    atrades = [
        {"symbol": "BTCUSDT", "side": "BUY", "qty": "0.001", "price": "60000", "commission": "0.012", "realizedPnl": "0", "time": 1000, "id": 1},
        {"symbol": "BTCUSDT", "side": "SELL", "qty": "0.001", "price": "60100", "commission": "0.012", "realizedPnl": "0.1", "time": 61000, "id": 2},
        {"symbol": "BTCUSDT", "side": "BUY", "qty": "0.001", "price": "60100", "commission": "0.012", "realizedPnl": "0", "time": 120000, "id": 3},
        {"symbol": "BTCUSDT", "side": "SELL", "qty": "0.001", "price": "59900", "commission": "0.012", "realizedPnl": "-0.2", "time": 180000, "id": 4},
    ]
    trips = reconstruct_trades(atrades, [{"time": 1500, "income": "-0.005"}])
    assert len(trips) == 2, trips
    win, lose = trips
    assert win.direction == "LONG" and abs(win.net_pnl - (0.1 - 0.024 - 0.005)) < 1e-9, win
    assert abs(win.duration_s - 60) < 1e-9 and lose.net_pnl < 0, (win, lose)
    a = summarize_trades(trips)
    assert (a["count"], a["wins"], a["losses"]) == (2, 1, 1), a
    print("  Análisis: reconstrucción + ganadora/perdedora      OK")
    print("OFFLINE: todo OK\n")


def _load_env() -> tuple[str | None, str | None]:
    try:
        from dotenv import load_dotenv

        root = Path(__file__).resolve().parents[2]  # .../app-bfuture
        load_dotenv(root / ".env")
    except Exception:
        pass
    key = os.getenv("BINANCE_API_KEY")
    sec = os.getenv("BINANCE_API_SECRET")
    placeholder = {None, "", "tu_api_key", "tu_api_secret"}
    if key in placeholder or sec in placeholder:
        return None, None
    return key, sec


async def check_online() -> None:
    print("== ONLINE (best-effort, TESTNET) ==")
    key, sec = _load_env()
    async with BinanceFuturesClient(key, sec, testnet=True) as cli:
        try:
            await cli.ping()
            offset = await cli.sync_time()
            info = await cli.exchange_info()
            cache = FilterCache(cli)
            await cache.refresh()
            print(f"  ping OK · offset reloj={offset}ms · símbolos PERPETUAL={len(cache)}")
            print(f"  rate-limit headers: {cli.rate_limits or '(sin datos)'}")
            if "BTCUSDT" in cache:
                f = cache.get("BTCUSDT")
                print(f"  BTCUSDT real -> tick={f.tick_size} step={f.step_size} "
                      f"minNotional={f.min_notional}")
        except BinanceError as e:
            print(f"  (sin red o endpoint inaccesible) {e}")
            return

        if key and sec:
            try:
                pos = await cli.position_risk()
                abiertas = [p for p in pos if float(p.get("positionAmt", 0)) != 0]
                print(f"  positionRisk v3 OK · posiciones abiertas={len(abiertas)}")
            except BinanceError as e:
                print(f"  positionRisk falló (revisa claves/permisos testnet): {e}")
        else:
            print("  (sin claves reales en .env -> se omite prueba firmada)")
    print("ONLINE: fin\n")


def main() -> None:
    check_offline()
    try:
        asyncio.run(check_online())
    except Exception as e:  # la parte online nunca debe tumbar la verificación
        print(f"ONLINE omitido: {e}")


if __name__ == "__main__":
    main()
