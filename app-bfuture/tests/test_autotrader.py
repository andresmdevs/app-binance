import asyncio
from decimal import Decimal

from binance import autotrader


def test_engine_config_from_args():
    a = autotrader.parse_args(["run", "--symbols", "SOLUSDT", "--margin", "3",
                               "--leverage", "4", "--min-score", "3", "--votes",
                               "trend", "macd", "--maker"])
    cfg = autotrader.engine_config(a)
    assert cfg.mode == "candle" and cfg.symbols == ["SOLUSDT"]
    assert cfg.margin_usdt == Decimal("3") and cfg.leverage == 4
    assert cfg.signal.min_score == 3 and cfg.signal.enabled == ("trend", "macd")
    assert cfg.entry_type == "MAKER" and cfg.lead_s == 4.0


def test_engine_config_radar_defaults():
    a = autotrader.parse_args(["run", "--mode", "radar"])
    cfg = autotrader.engine_config(a)
    assert cfg.mode == "radar" and cfg.symbols == [] and cfg.interval == "5m"


class _Hist:
    """Sirve velas de 1m en páginas de como mucho `limit`."""
    def __init__(self):
        self.calls = 0

    async def klines(self, symbol, interval, limit=500, startTime=None, endTime=None):
        self.calls += 1
        out, t = [], startTime
        while t <= endTime and len(out) < limit:
            out.append([t, 1, 1, 1, 1, 1, t + 59_999, 1, 1, 0.5, 0.5, 0])
            t += 60_000
        return out


def test_fetch_history_paginates_closed_candles():
    h = _Hist()
    kl = asyncio.run(autotrader.fetch_history(h, "BTCUSDT", "1m", days=2))
    assert len(kl) == 2 * 1440 and h.calls == 2      # 2880 velas = 1500 + 1380
    opens = [k[0] for k in kl]
    assert opens == sorted(set(opens))                 # sin duplicados ni huecos
