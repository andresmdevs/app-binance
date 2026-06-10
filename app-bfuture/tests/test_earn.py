import asyncio

from binance.earn import (EarnProduct, make_spot_client, parse_flexible_positions,
                          parse_products, summarize_positions, top_products, _paged)

PRODUCTS = [
    {"productId": "HOME001", "asset": "HOME", "latestAnnualPercentageRate": "0.35",
     "canPurchase": True, "minPurchaseAmount": "0.1",
     "tierAnnualPercentageRate": {"0-100HOME": "0.20"}},
    {"productId": "USDT001", "asset": "USDT", "latestAnnualPercentageRate": "0.052",
     "canPurchase": True, "minPurchaseAmount": "0.1"},
    {"productId": "OFF001", "asset": "OFF", "latestAnnualPercentageRate": "0.99",
     "canPurchase": False, "minPurchaseAmount": "1"},
]

POSITIONS = [
    {"asset": "USDT", "totalAmount": "12.5", "latestAnnualPercentageRate": "0.052",
     "yesterdayRealTimeRewards": "0.0017", "cumulativeTotalRewards": "0.41"},
    {"asset": "HOME", "totalAmount": "100", "latestAnnualPercentageRate": "0.35",
     "yesterdayRealTimeRewards": "0.09", "cumulativeTotalRewards": "1.2"},
]


def test_parse_products():
    ps = parse_products(PRODUCTS)
    assert len(ps) == 3
    home = ps[0]
    assert home.asset == "HOME" and abs(home.apr - 0.35) < 1e-9
    assert home.has_bonus_tiers is True and ps[1].has_bonus_tiers is False


def test_top_products_orders_and_marks_mine():
    ps = parse_products(PRODUCTS)
    top = top_products(ps, limit=5, held_assets={"USDT"})
    # OFF (0.99) queda fuera por canPurchase=False; HOME (0.35) primero
    assert [t["product"].asset for t in top] == ["HOME", "USDT"]
    assert top[1]["mine"] is True and top[0]["mine"] is False


def test_parse_and_summarize_positions():
    pos = parse_flexible_positions(POSITIONS)
    s = summarize_positions(pos)
    assert s["count"] == 2 and s["assets"] == ["HOME", "USDT"]
    assert abs(s["best_apr_held"] - 0.35) < 1e-9
    assert abs(s["yesterday_total_by_asset"]["USDT"] - 0.0017) < 1e-12


def test_spot_client_base():
    c = make_spot_client("k", "s")
    assert c.base_url == "https://api.binance.com"


class _PagedFake:
    """Devuelve 100 filas en la página 1 y 3 en la 2 (corta la paginación)."""
    def __init__(self):
        self.pages = []

    async def _request(self, method, path, params=None, *, signed=False):
        self.pages.append(params["current"])
        n = 100 if params["current"] == 1 else 3
        return {"rows": [{"i": i} for i in range(n)]}


def test_paged_stops_when_short_page():
    c = _PagedFake()
    rows = asyncio.run(_paged(c, "/x"))
    assert len(rows) == 103 and c.pages == [1, 2]
