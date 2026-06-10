import asyncio

from binance.market import WINDOWS, parse_tickers, summarize_market, top_movers

ROWS = [
    {"symbol": "BTCUSDT", "lastPrice": "60000", "priceChangePercent": "2.5", "quoteVolume": "1000"},
    {"symbol": "ETHUSDT", "lastPrice": "3000", "priceChangePercent": "-1.0", "quoteVolume": "800"},
    {"symbol": "XRPUSDT", "lastPrice": "0.5", "priceChangePercent": "5.0", "quoteVolume": "200"},
    {"symbol": "NOPEUSDT", "lastPrice": "1", "priceChangePercent": "9.0", "quoteVolume": "9"},
]


def test_parse_allowed():
    assert len(parse_tickers(ROWS, allowed={"BTCUSDT", "ETHUSDT", "XRPUSDT"})) == 3


def test_summary():
    s = summarize_market(parse_tickers(ROWS))
    assert s.advancers == 3 and s.decliners == 1
    assert s.top_gainer.symbol == "NOPEUSDT" and s.top_loser.symbol == "ETHUSDT"
    assert abs(s.total_quote_volume - 2009) < 1e-9


def test_top_movers_1d():
    m = asyncio.run(top_movers(None, parse_tickers(ROWS), window="1d", limit=2, universe=10))
    assert [x.symbol for x in m] == ["NOPEUSDT", "XRPUSDT"]


def test_windows():
    assert set(WINDOWS) == {"1h", "4h", "1d", "5d", "1month"}
