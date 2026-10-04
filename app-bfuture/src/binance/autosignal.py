"""Señal autónoma por vela: cada indicador VOTA (estilo modo autónomo de Exnova).

Unos segundos antes de que cierre la vela (= antes de que abra la siguiente),
cada indicador vota +1 (long), -1 (short) o 0 (sin opinión) y la suma decide:

    trend      EMA 9 vs EMA 21                 sigue la tendencia
    macd       histograma MACD (12/26/9)       sigue el impulso
    rsi        RSI 14 < 30 -> +1 / > 70 -> -1  espera rebote en extremos
    bollinger  cierre fuera de la banda 20·2σ  espera rebote en extremos
    flow       % de volumen taker comprador     presión agresiva (sustituye al
               de las últimas velas             "resumen técnico" de Exnova)

Diferencias con Exnova, porque aquí NO son opciones binarias:

  * Sin desempate por color de vela: si la suma no llega a ``min_score`` NO se
    opera (en futuros saltarse una vela es gratis; en Exnova ese desempate
    acertó 33%).
  * Gate de volatilidad vs comisiones: el TP (múltiplo del ATR) tiene que cubrir
    varias veces la comisión ida+vuelta. En velas de 1m de pares tranquilos el
    movimiento esperado no paga las comisiones, y entonces no se opera.
  * TP/SL por ATR + salida por tiempo -> riesgo definido. Sin martingala.

Todo es PURO (sin red): el motor en vivo (`engine`) y el replay histórico
(`simulate`) llaman a la MISMA ``decide``, así que lo que se mide es lo que opera.

Formato de kline: [openTime, open, high, low, close, volume, closeTime,
quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore].
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from .analysis import ema_series, rsi_last

# Velas por análisis: <100 -> peso 1 en /fapi/v1/klines. El replay usa la misma
# ventana para que las EMAs arranquen igual que en vivo.
KLINES_LIMIT = 99

VOTE_NAMES = ("trend", "macd", "rsi", "bollinger", "flow")
VOTE_LABELS = {
    "trend": "Tendencia EMA 9/21",
    "macd": "MACD 12/26/9",
    "rsi": "RSI 14",
    "bollinger": "Bollinger 20·2σ",
    "flow": "Flujo taker",
    "decision": "Decisión combinada",
}


@dataclass
class SignalConfig:
    ema_fast: int = 9
    ema_slow: int = 21
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    rsi_low: float = 30.0
    rsi_high: float = 70.0
    bb_period: int = 20
    bb_k: float = 2.0
    flow_bars: int = 5            # velas para el ratio taker comprador
    flow_band: float = 0.05       # >55% compra agresiva -> +1 ; <45% -> -1
    atr_period: int = 14
    enabled: tuple = VOTE_NAMES   # votos que cuentan para la suma
    min_score: int = 2            # |suma| mínima para operar (sin desempates)
    tp_atr: float = 1.5           # take-profit = tp_atr × ATR
    sl_atr: float = 1.0           # stop-loss   = sl_atr × ATR
    fee_rt: float = 0.001         # comisión ida+vuelta (taker 0.05% × 2)
    min_edge_fees: float = 2.0    # el TP debe cubrir >= N × fee_rt
    min_bars: int = 60            # velas mínimas para decidir


@dataclass
class Decision:
    side: Optional[str]           # "BUY" | "SELL" | None (no operar)
    score: int
    votes: dict
    values: dict
    price: float
    atr_pct: float
    tp_pct: float                 # fracción de precio
    sl_pct: float
    reason: str


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


# --- Indicadores (listas de floats, sin pandas) -------------------------------
def macd_hist(closes: list[float], fast: int = 12, slow: int = 26,
              signal: int = 9) -> Optional[float]:
    """Histograma MACD (línea - señal) de la última vela."""
    if len(closes) < slow + signal:
        return None
    line = [a - b for a, b in zip(ema_series(closes, fast), ema_series(closes, slow))]
    return line[-1] - ema_series(line, signal)[-1]


def bollinger(closes: list[float], period: int = 20,
              k: float = 2.0) -> Optional[tuple[float, float, float]]:
    """(banda inferior, media, banda superior) con desviación poblacional."""
    if len(closes) < period:
        return None
    w = closes[-period:]
    mid = sum(w) / period
    sd = math.sqrt(sum((x - mid) ** 2 for x in w) / period)
    return mid - k * sd, mid, mid + k * sd


def atr(highs: list[float], lows: list[float], closes: list[float],
        period: int = 14) -> Optional[float]:
    """Average True Range con suavizado de Wilder."""
    if len(closes) < period + 1:
        return None
    trs = [max(h - lo, abs(h - pc), abs(lo - pc))
           for h, lo, pc in zip(highs[1:], lows[1:], closes[:-1])]
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def taker_buy_ratio(klines: list[list], bars: int = 5) -> Optional[float]:
    """Fracción del volumen de las últimas `bars` velas que fue compra agresiva."""
    rows = [k for k in klines[-bars:] if len(k) > 9]
    vol = sum(float(k[5]) for k in rows)
    if vol <= 0:
        return None
    return sum(float(k[9]) for k in rows) / vol


# --- Votos + decisión ----------------------------------------------------------
def indicator_votes(klines: list[list],
                    cfg: Optional[SignalConfig] = None) -> tuple[dict, dict]:
    """Voto de CADA indicador (todos, aunque no estén habilitados: el marcador en
    sombra los mide igual) + los valores crudos para el diario."""
    cfg = cfg or SignalConfig()
    closes = [float(k[4]) for k in klines]
    price = closes[-1]
    ef = ema_series(closes, cfg.ema_fast)[-1]
    es = ema_series(closes, cfg.ema_slow)[-1]
    hist = macd_hist(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    r = rsi_last(closes, cfg.rsi_period)
    bb = bollinger(closes, cfg.bb_period, cfg.bb_k)
    flow = taker_buy_ratio(klines, cfg.flow_bars)
    votes = {
        "trend": _sign(ef - es),
        "macd": 0 if hist is None else _sign(hist),
        "rsi": 0 if r is None else (1 if r < cfg.rsi_low else -1 if r > cfg.rsi_high else 0),
        "bollinger": 0 if bb is None else (1 if price < bb[0] else -1 if price > bb[2] else 0),
        "flow": 0 if flow is None else (1 if flow > 0.5 + cfg.flow_band
                                        else -1 if flow < 0.5 - cfg.flow_band else 0),
    }
    values = {
        "ema_fast": ef, "ema_slow": es, "macd_hist": hist,
        "rsi": None if r is None else round(r, 1),
        "bb_low": None if bb is None else bb[0], "bb_high": None if bb is None else bb[2],
        "taker_buy": None if flow is None else round(flow, 3),
    }
    return votes, values


def decide(klines: list[list], cfg: Optional[SignalConfig] = None) -> Decision:
    """Decisión de la vela: lado a operar (o None) con TP/SL por ATR y el motivo."""
    cfg = cfg or SignalConfig()
    if len(klines) < cfg.min_bars:
        price = float(klines[-1][4]) if klines else 0.0
        return Decision(None, 0, {}, {}, price, 0.0, 0.0, 0.0,
                        f"datos insuficientes ({len(klines)}/{cfg.min_bars} velas)")
    votes, values = indicator_votes(klines, cfg)
    score = sum(votes.get(k, 0) for k in cfg.enabled)
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    price = closes[-1]
    a = atr(highs, lows, closes, cfg.atr_period)
    atr_pct = (a / price) if (a and price) else 0.0
    tp_pct, sl_pct = cfg.tp_atr * atr_pct, cfg.sl_atr * atr_pct
    values["atr_pct"] = round(atr_pct * 100, 4)

    side = None
    if abs(score) < cfg.min_score:
        reason = f"señal débil: suma {score:+d} (mínimo ±{cfg.min_score})"
    elif sl_pct <= 0:
        reason = "sin volatilidad medible (ATR 0)"
    elif tp_pct < cfg.min_edge_fees * cfg.fee_rt:
        reason = (f"poca volatilidad: TP {tp_pct * 100:.3f}% < {cfg.min_edge_fees:g}× "
                  f"comisión ida+vuelta ({cfg.fee_rt * 100:.2f}%)")
    else:
        side = "BUY" if score > 0 else "SELL"
        d = _sign(score)
        pro = [VOTE_LABELS[k] for k in cfg.enabled if votes.get(k) == d]
        reason = f"suma {score:+d}: " + ", ".join(pro)
    return Decision(side, score, votes, values, price, atr_pct, tp_pct, sl_pct, reason)


# --- Marcador en sombra ----------------------------------------------------------
def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalo de confianza (95%) de una tasa de acierto: con pocas muestras
    es ancho, y eso es justo lo que hay que ver antes de creerse un %."""
    if n <= 0:
        return 0.0, 1.0
    p = hits / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


@dataclass
class Tally:
    hits: int = 0
    total: int = 0

    @property
    def rate(self) -> Optional[float]:
        return self.hits / self.total if self.total else None


class ShadowBoard:
    """Marcador en sombra: en cada vela anota qué votó cada indicador (y la
    decisión combinada) y, cuando esa vela cierra, comprueba si acertó la
    dirección. NO opera: sirve para saber qué indicadores aportan y cuáles
    estorban, con datos de TODAS las velas (no solo las operadas)."""

    def __init__(self, max_pending: int = 2000):
        self.tallies: dict[str, Tally] = {n: Tally() for n in (*VOTE_NAMES, "decision")}
        self._pending: dict[tuple, tuple] = {}
        self._max_pending = max_pending

    def record(self, symbol: str, open_time: int, votes: dict, side: Optional[str]) -> None:
        """Predicción para la vela que abre en `open_time`."""
        self._pending[(symbol, int(open_time))] = (dict(votes), side)
        while len(self._pending) > self._max_pending:
            self._pending.pop(next(iter(self._pending)))

    def resolve(self, symbol: str, open_time: int, open_price: float,
                close_price: float) -> bool:
        item = self._pending.pop((symbol, int(open_time)), None)
        if item is None:
            return False
        direction = _sign(close_price - open_price)
        if direction == 0:
            return True  # vela plana: no cuenta ni a favor ni en contra
        votes, side = item
        for name, v in votes.items():
            t = self.tallies.get(name)
            if t is not None and v != 0:
                t.total += 1
                t.hits += int(v == direction)
        if side:
            t = self.tallies["decision"]
            t.total += 1
            t.hits += int((1 if side == "BUY" else -1) == direction)
        return True

    def resolve_klines(self, symbol: str, klines: list[list], now_ms: int) -> None:
        """Resuelve las predicciones pendientes cuyas velas ya cerraron."""
        for k in klines:
            if int(k[6]) < now_ms:
                self.resolve(symbol, int(k[0]), float(k[1]), float(k[4]))

    def ranking(self) -> list[dict]:
        """Indicadores ordenados por el extremo INFERIOR del intervalo (lo que se
        puede afirmar con los datos), no por el % bruto."""
        rows = []
        for name, t in self.tallies.items():
            lo, hi = wilson_interval(t.hits, t.total)
            rows.append({"name": name, "label": VOTE_LABELS.get(name, name),
                         "hits": t.hits, "total": t.total, "rate": t.rate,
                         "lo": lo, "hi": hi})
        rows.sort(key=lambda r: (r["total"] > 0, r["lo"]), reverse=True)
        return rows


# --- Replay histórico (mismas reglas que en vivo) --------------------------------
@dataclass
class SimTrade:
    open_time: int
    side: str
    entry: float
    exit: float
    reason: str                   # "tp" | "sl" | "tiempo"
    net_pct: float                # neto sobre el notional (tras comisiones+slippage)
    score: int


def simulate(klines: list[list], cfg: Optional[SignalConfig] = None, *,
             hold_candles: int = 1, slippage_bps: float = 1.0,
             window: int = KLINES_LIMIT, symbol: str = "SIM",
             shadow: Optional[ShadowBoard] = None) -> list[SimTrade]:
    """Replay vela a vela con la MISMA ``decide`` del motor en vivo.

    Decide con la vela i (en vivo: 4 s antes de su cierre), entra en la apertura
    de i+1 y sale por TP/SL intra-vela (si ambos se tocan en la misma vela, se
    asume el PEOR caso: stop) o al cierre de la vela i+hold. Descuenta comisión
    ida+vuelta y slippage por lado. Una posición a la vez (como en vivo, una por
    símbolo). Si se pasa `shadow`, también lo alimenta con TODAS las velas.
    """
    cfg = cfg or SignalConfig()
    hold = max(1, int(hold_candles))
    slip = slippage_bps / 10_000.0
    n = len(klines)
    trades: list[SimTrade] = []
    busy_until = -1
    for i in range(max(0, cfg.min_bars - 1), n - 1):
        d = decide(klines[max(0, i + 1 - window): i + 1], cfg)
        nxt = klines[i + 1]
        if shadow is not None:
            shadow.record(symbol, int(nxt[0]), d.votes, d.side)
            shadow.resolve(symbol, int(nxt[0]), float(nxt[1]), float(nxt[4]))
        last = i + hold
        if d.side is None or i < busy_until or last > n - 1:
            continue
        s = 1 if d.side == "BUY" else -1
        entry = float(nxt[1]) * (1 + s * slip)
        tp = entry * (1 + s * d.tp_pct)
        sl = entry * (1 - s * d.sl_pct)
        exit_px, reason, exit_bar = None, "tiempo", last
        for j in range(i + 1, last + 1):
            hi, lo = float(klines[j][2]), float(klines[j][3])
            hit_sl = lo <= sl if s == 1 else hi >= sl
            hit_tp = hi >= tp if s == 1 else lo <= tp
            if hit_sl:                       # peor caso: el stop va primero
                exit_px, reason, exit_bar = sl, "sl", j
                break
            if hit_tp:
                exit_px, reason, exit_bar = tp, "tp", j
                break
        if exit_px is None:
            exit_px = float(klines[last][4])
        exit_px *= (1 - s * slip)
        net = s * (exit_px - entry) / entry - cfg.fee_rt
        trades.append(SimTrade(int(nxt[0]), d.side, entry, exit_px, reason, net, d.score))
        busy_until = exit_bar
    return trades


def max_losing_streak(nets: list[float]) -> int:
    best = cur = 0
    for x in nets:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    return best


def summarize_sim(trades: list[SimTrade], *, margin: float = 1.0,
                  leverage: float = 1.0) -> dict:
    """Resumen del replay. `pnl_usdt` = suma de net_pct × margen × apalancamiento."""
    n = len(trades)
    if not n:
        return {"trades": 0}
    nets = [t.net_pct for t in trades]
    gp = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x <= 0)
    wins = sum(1 for x in nets if x > 0)
    lo, hi = wilson_interval(wins, n)
    return {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n,
        "win_rate_lo": lo,
        "win_rate_hi": hi,
        "avg_net_pct": sum(nets) / n,
        "total_net_pct": sum(nets),
        "profit_factor": (gp / gl) if gl else float("inf"),
        "max_consec_losses": max_losing_streak(nets),
        "by_reason": dict(Counter(t.reason for t in trades)),
        "longs": sum(1 for t in trades if t.side == "BUY"),
        "shorts": sum(1 for t in trades if t.side == "SELL"),
        "pnl_usdt": sum(nets) * margin * leverage,
    }
