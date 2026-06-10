import asyncio
from decimal import Decimal

from binance.scalp import ScalpConfig, bracket_prices, open_scalp
from binance.trade import FuturesTrader


def run(coro):
    return asyncio.run(coro)


def test_bracket_prices_long():
    tp, sl = bracket_prices("BUY", 100, 0.01, 0.005)
    assert tp == Decimal("101") and sl == Decimal("99.5")


def test_bracket_prices_short():
    tp, sl = bracket_prices("SELL", 100, 0.01, 0.005)
    assert tp == Decimal("99") and sl == Decimal("100.5")


def test_open_scalp_places_entry_and_bracket(filters, make_fake_client):
    c = make_fake_client(mark="60000")
    cfg = ScalpConfig(notional=Decimal("120"), take_profit_pct=0.01,
                      stop_loss_pct=0.005, max_hold_seconds=300, leverage=10)
    res = run(open_scalp(FuturesTrader(c, filters), c,
                         symbol="BTCUSDT", side="BUY", config=cfg))
    assert res["errors"] == {}
    # entrada MARKET en /fapi/v1/order
    assert c.find("POST", "/fapi/v1/order")
    # bracket: TP + SL en el endpoint Algo, ambos cierran y lado opuesto
    algos = c.find("POST", "/fapi/v1/algoOrder")
    assert sorted(a["params"]["type"] for a in algos) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    for a in algos:
        assert a["params"]["side"] == "SELL" and a["params"]["closePosition"] == "true"
    # leverage configurado
    assert c.find("POST", "/fapi/v1/leverage")


def test_open_scalp_maker_uses_limit_gtx(filters, make_fake_client):
    c = make_fake_client(mark="60000")
    cfg = ScalpConfig(notional=Decimal("120"), take_profit_pct=0.01, stop_loss_pct=0.005,
                      leverage=10, entry_type="MAKER", entry_timeout_s=1)
    res = run(open_scalp(FuturesTrader(c, filters), c,
                         symbol="BTCUSDT", side="BUY", config=cfg))
    assert res["filled"] is True and res["errors"] == {}
    # entrada LIMIT post-only (GTX), no MARKET
    orders = c.find("POST", "/fapi/v1/order")
    assert orders and orders[0]["params"]["type"] == "LIMIT"
    assert orders[0]["params"]["timeInForce"] == "GTX"
    # bracket TP+SL colocado tras el fill
    algos = c.find("POST", "/fapi/v1/algoOrder")
    assert sorted(a["params"]["type"] for a in algos) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
