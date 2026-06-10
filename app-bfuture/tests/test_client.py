import time

from binance.client import (MAINNET_REST, MAINNET_WS, TESTNET_REST, TESTNET_WS,
                            BinanceAPIError, BinanceFuturesClient)


def test_bases():
    c = BinanceFuturesClient("k", "s", testnet=True)
    assert c.base_url == TESTNET_REST and c.ws_base == TESTNET_WS
    c2 = BinanceFuturesClient("k", "s", testnet=False)
    assert c2.base_url == MAINNET_REST and c2.ws_base == MAINNET_WS


def test_recv_window_clamped():
    assert BinanceFuturesClient("k", "s", recv_window=99999).recv_window == 60000


def test_sign_deterministic():
    c = BinanceFuturesClient("k", "secret")
    q = c._sign({"symbol": "BTCUSDT", "side": "BUY"})
    sig = q.split("signature=")[1]
    assert len(sig) == 64 and all(ch in "0123456789abcdef" for ch in sig)
    assert c._sign({"symbol": "BTCUSDT", "side": "BUY"}) == q  # determinista


def test_timestamp_offset():
    c = BinanceFuturesClient("k", "s")
    c.time_offset = 10_000
    assert c._timestamp() >= int(time.time() * 1000) + 9_000


def test_parse_retry_after():
    assert BinanceFuturesClient._parse_retry_after({"Retry-After": "7"}) == 7.0
    assert BinanceFuturesClient._parse_retry_after({}) is None


def test_backoff():
    c = BinanceFuturesClient("k", "s")
    assert c._backoff_delay(0, 5.0) == 5.0       # respeta Retry-After
    assert c._backoff_delay(2, None) == 4.0      # exponencial


def test_capture_rate_limits():
    c = BinanceFuturesClient("k", "s")
    c._capture_rate_limits({"X-MBX-USED-WEIGHT-1M": "42",
                            "X-MBX-ORDER-COUNT-1M": "3", "Other": "x"})
    assert c.rate_limits["X-MBX-USED-WEIGHT-1M"] == 42
    assert c.rate_limits["X-MBX-ORDER-COUNT-1M"] == 3
    assert "OTHER" not in c.rate_limits


def test_api_error_attrs():
    e = BinanceAPIError(400, -1111, "bad precision")
    assert e.status == 400 and e.code == -1111 and "bad precision" in str(e)
