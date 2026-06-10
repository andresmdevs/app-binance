import asyncio

from binance.client import BinanceUnknownStatus
from binance.trade import FuturesTrader


def run(coro):
    return asyncio.run(coro)


def test_market_uses_order_endpoint(filters, make_fake_client):
    c = make_fake_client()
    run(FuturesTrader(c, filters).market("BTCUSDT", "BUY", 0.002))
    calls = c.find("POST", "/fapi/v1/order")
    assert len(calls) == 1
    p = calls[0]["params"]
    assert p["type"] == "MARKET" and p["side"] == "BUY" and p["quantity"] == "0.002"
    assert "price" not in p


def test_limit_uses_order_endpoint(filters, make_fake_client):
    c = make_fake_client()
    run(FuturesTrader(c, filters).limit("BTCUSDT", "BUY", 0.002, 59000))
    p = c.find("POST", "/fapi/v1/order")[0]["params"]
    assert p["type"] == "LIMIT" and p["price"] == "59000.0" and p["timeInForce"] == "GTC"


def test_stop_market_uses_algo_endpoint(filters, make_fake_client):
    """Regresión del cambio 2025-12-09: condicionales -> /fapi/v1/algoOrder."""
    c = make_fake_client()
    run(FuturesTrader(c, filters).stop_market("BTCUSDT", "SELL", 55000, close_position=True))
    assert not c.find("POST", "/fapi/v1/order")
    algo = c.find("POST", "/fapi/v1/algoOrder")
    assert len(algo) == 1
    p = algo[0]["params"]
    assert p["algoType"] == "CONDITIONAL" and p["type"] == "STOP_MARKET"
    assert p["triggerPrice"] == "55000.0" and p["closePosition"] == "true"
    assert "clientAlgoId" in p and "newClientOrderId" not in p


def test_place_order_routes_conditional(filters, make_fake_client):
    c = make_fake_client()
    run(FuturesTrader(c, filters).place_order(
        symbol="BTCUSDT", side="SELL", order_type="STOP",
        quantity=0.002, price=59000, stop_price=58900, mark_price=60000))
    assert c.find("POST", "/fapi/v1/algoOrder") and not c.find("POST", "/fapi/v1/order")


def test_close_market_sets_reduce_only(filters, make_fake_client):
    c = make_fake_client()
    run(FuturesTrader(c, filters).close_market("BTCUSDT", "SELL", 0.002))
    assert c.find("POST", "/fapi/v1/order")[0]["params"].get("reduceOnly") == "true"


def test_cancel_endpoints(filters, make_fake_client):
    c = make_fake_client()
    t = FuturesTrader(c, filters)
    run(t.cancel_order("BTCUSDT", order_id=1))
    run(t.cancel_algo_order(algo_id=2))
    assert c.find("DELETE", "/fapi/v1/order") and c.find("DELETE", "/fapi/v1/algoOrder")


def test_audit_records(filters, make_fake_client):
    c = make_fake_client()
    rec = []

    class A:
        def record(self, action, *a):
            rec.append(action)

    run(FuturesTrader(c, filters, audit=A()).market("BTCUSDT", "BUY", 0.002))
    assert "order" in rec and "order.ok" in rec


def test_reconcile_on_unknown_status(filters, make_fake_client):
    c = make_fake_client(
        raise_on={("POST", "/fapi/v1/order"): BinanceUnknownStatus("timeout")},
        responses={"/fapi/v1/order": {"orderId": 1, "status": "FILLED", "clientOrderId": "x"}})
    res = run(FuturesTrader(c, filters).market("BTCUSDT", "BUY", 0.002))
    assert res["status"] == "FILLED"  # reconciliado vía query, no duplicado


def test_open_with_protection(filters, make_fake_client):
    c = make_fake_client()
    res = run(FuturesTrader(c, filters).open_with_protection(
        "BTCUSDT", "BUY", 0.002, stop_pct=0.02, mark_price=60000))
    assert res["stop_error"] is None
    assert c.find("POST", "/fapi/v1/order") and c.find("POST", "/fapi/v1/algoOrder")
