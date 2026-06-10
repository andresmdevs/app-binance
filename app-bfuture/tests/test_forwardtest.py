from binance.forwardtest import select_candidates
from binance.market import parse_tickers

ROWS = [
    {"symbol": "CHEAPUSDT", "lastPrice": "0.20", "priceChangePercent": "30", "quoteVolume": "2000000"},
    {"symbol": "MIDUSDT", "lastPrice": "0.40", "priceChangePercent": "10", "quoteVolume": "5000000"},
    {"symbol": "EXPENSIVEUSDT", "lastPrice": "5", "priceChangePercent": "50", "quoteVolume": "9000000"},
    {"symbol": "DOWNUSDT", "lastPrice": "0.10", "priceChangePercent": "-5", "quoteVolume": "3000000"},
    {"symbol": "ILLIQUSDT", "lastPrice": "0.05", "priceChangePercent": "20", "quoteVolume": "100"},
]
VALID = {"CHEAPUSDT", "MIDUSDT", "EXPENSIVEUSDT", "DOWNUSDT", "ILLIQUSDT"}


def test_filters_and_orders_by_strength():
    t = parse_tickers(ROWS)
    c = select_candidates(t, VALID, max_price=0.5, min_quote_volume=1_000_000, top_n=5)
    # caro (>0.5), bajando, e ilíquido quedan fuera; orden por cambio% desc
    assert c == ["CHEAPUSDT", "MIDUSDT"]


def test_excludes_held():
    t = parse_tickers(ROWS)
    c = select_candidates(t, VALID, max_price=0.5, min_quote_volume=1_000_000,
                          top_n=5, exclude={"CHEAPUSDT"})
    assert c == ["MIDUSDT"]


def test_respects_valid_symbols_and_top_n():
    t = parse_tickers(ROWS)
    c = select_candidates(t, {"MIDUSDT"}, max_price=0.5, min_quote_volume=1_000_000, top_n=5)
    assert c == ["MIDUSDT"]
    c2 = select_candidates(t, VALID, max_price=0.5, min_quote_volume=1_000_000, top_n=1)
    assert c2 == ["CHEAPUSDT"]
