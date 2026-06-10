"""Descarga y cacheo de velas (klines) de Binance USDⓈ-M Futures.

Fuente: endpoint público REST /fapi/v1/klines (NO requiere API key, solo lectura).
Las velas se cachean en backtest/.cache/ para que las re-ejecuciones sean instantáneas.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

FAPI_BASE = "https://fapi.binance.com"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

# milisegundos por intervalo (para paginar)
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}

_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def _to_ms(ts) -> int:
    """Convierte cualquier timestamp (naive o tz-aware) a epoch ms en UTC."""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return int(t.timestamp() * 1000)


def _get_json(url: str, params: dict, retries: int = 5):
    qs = urllib.parse.urlencode(params)
    full = f"{url}?{qs}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                full, headers={"User-Agent": "bfuture-backtest/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 418):  # rate limit / ban temporal
                wait = 2 ** attempt
                print(f"  rate-limited ({e.code}), esperando {wait}s...")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            wait = 1.5 * (attempt + 1)
            print(f"  error de red ({e}), reintentando en {wait:.0f}s...")
            time.sleep(wait)
    raise RuntimeError(f"No se pudo obtener datos tras {retries} intentos: {full}")


def _download(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    if interval not in _INTERVAL_MS:
        raise ValueError(f"Intervalo no soportado: {interval}")
    step = _INTERVAL_MS[interval]
    rows: list = []
    cur = start_ms
    while cur < end_ms:
        data = _get_json(
            f"{FAPI_BASE}/fapi/v1/klines",
            {
                "symbol": symbol, "interval": interval,
                "startTime": cur, "endTime": end_ms, "limit": 1500,
            },
        )
        if not data:
            break
        rows.extend(data)
        nxt = data[-1][0] + step  # avanzar tras el último openTime
        if nxt <= cur:            # seguro anti-bucle
            break
        cur = nxt
        if len(data) < 1500:      # ya no hay más datos
            break
        time.sleep(0.25)          # cortesía con la API
    return pd.DataFrame(rows, columns=_COLUMNS) if rows else pd.DataFrame(columns=_COLUMNS)


def get_klines(symbol: str, interval: str, start, end, use_cache: bool = True) -> pd.DataFrame:
    """Devuelve un DataFrame indexado por tiempo (UTC) con columnas
    open/high/low/close/volume.
    """
    start_ms, end_ms = _to_ms(start), _to_ms(end)
    cache_file = CACHE_DIR / f"{symbol}_{interval}_{start_ms}_{end_ms}.csv"

    if use_cache and cache_file.exists():
        df = pd.read_csv(cache_file)
    else:
        print(f"Descargando {symbol} {interval} ({pd.Timestamp(start_ms, unit='ms').date()} "
              f"→ {pd.Timestamp(end_ms, unit='ms').date()}) ...")
        df = _download(symbol, interval, start_ms, end_ms)
        if df.empty:
            raise RuntimeError(f"No se obtuvieron velas para {symbol} {interval}.")
        df = df.drop_duplicates(subset="open_time").sort_values("open_time")
        df.to_csv(cache_file, index=False)
        print(f"  {len(df)} velas guardadas en cache.")

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("dt")
    return df[["open", "high", "low", "close", "volume"]].dropna()
