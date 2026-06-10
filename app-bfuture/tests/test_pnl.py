from binance.pnl import filter_events, income_rows_to_events, summarize

ROWS = [
    {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "12.5", "time": 1700000000000, "tradeId": 1},
    {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-4.0", "time": 1700000100000, "tradeId": 2},
    {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "3.0", "time": 1700000200000, "tradeId": 3},
    {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.5", "time": 1700000050000},
]


def test_events_filtered():
    assert len(income_rows_to_events(ROWS)) == 3  # COMMISSION ignorado


def test_summarize():
    s = summarize(income_rows_to_events(ROWS))
    assert (s.count, s.wins, s.losses) == (3, 2, 1)
    assert abs(s.total - 11.5) < 1e-9
    assert abs(s.profit_factor - 3.875) < 1e-9
    assert s.best == 12.5 and s.worst == -4.0


def test_filters():
    ev = income_rows_to_events(ROWS)
    assert len(filter_events(ev, only_positive=True)) == 2
    assert len(filter_events(ev, symbol="BTCUSDT")) == 2
    assert len(filter_events(ev, min_pnl=5)) == 1
