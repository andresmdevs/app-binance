"""Motor de señales: detección de estructura + scoring de oportunidades.

TODO lo de este módulo son FUNCIONES PURAS (sin red): reciben datos ya
descargados (klines, prints de liquidación, filas de OI/taker) y devuelven
zonas, sesgo, R:R y un score compuesto. Así se testea sin tocar Binance y el
`main`/UI solo se encarga de traer los datos y pintar el resultado.

Pipeline conceptual (ver discusión de diseño):

    1. Estructura   -> volume_profile -> sr_zones (soportes/resistencias imán)
    2. Liquidación  -> cluster_liquidations / estimated_liq_levels (combustible)
    3. Flujo/sesgo  -> directional_bias (OI + funding + taker ratio)
    4. Perfil       -> profile_fit (parecido a tus ganadoras vs perdedoras)
    5. Calidad      -> net_rr (R:R DESPUÉS de fees)
    Gates duros descartan; composite_score ordena; rank_setups deja 1-3.

Formato de kline de Binance: [openTime, open, high, low, close, volume, closeTime,
quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]. Se usan high(2),
low(3), close(4) y quoteVolume(7) en USDT.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

# --- Estructuras --------------------------------------------------------------
@dataclass
class PriceBin:
    low: float
    high: float
    center: float
    volume: float  # quoteVolume (USDT) acumulado en el bin


@dataclass
class Zone:
    """Zona de precio imán (soporte/resistencia por volumen)."""
    low: float
    high: float
    strength: float          # 0..1, normalizado sobre el nodo más fuerte
    kind: str                # "support" | "resistance"

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class LiqCluster:
    low: float
    high: float
    qty: float
    side: Optional[str]      # lado liquidado dominante ("long"/"short")

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class Bias:
    score: float             # -1 (short) .. +1 (long)
    label: str               # "long" | "short" | "neutral"
    notes: list[str] = field(default_factory=list)


@dataclass
class Gates:
    """Filtros duros: si falla uno, el candidato NO aparece."""
    min_rr: float = 1.5
    min_quote_volume: float = 1_000_000.0


@dataclass
class Setup:
    symbol: str
    side: str                # "long" | "short"
    entry_low: float
    entry_high: float
    stop: float
    target: float
    rr_net: float
    score: float             # 0..100
    components: dict = field(default_factory=dict)
    reason: str = ""


# Pesos del score compuesto (suman 100). Ajustables sin tocar la lógica.
DEFAULT_WEIGHTS: dict[str, float] = {
    "structure": 30.0,       # cercanía/fuerza de la zona
    "flow": 25.0,            # sesgo por OI + funding + taker
    "liquidity_fuel": 20.0,  # cluster de liquidación como imán
    "profile_fit": 15.0,     # parecido a tus ganadoras
    "rr_quality": 10.0,      # calidad del R:R neto
}


# --- 1. Estructura: volume profile + zonas S/R -------------------------------
def volume_profile(klines: Iterable[list], bins: int = 50) -> list[PriceBin]:
    """Reparte el quoteVolume de cada vela sobre el rango [low, high] que cubre,
    en `bins` cortes de precio. Los picos son nodos de alto volumen (imanes).
    """
    rows = [(float(k[3]), float(k[2]), float(k[7])) for k in klines]  # low, high, qvol
    if not rows:
        return []
    pmin = min(r[0] for r in rows)
    pmax = max(r[1] for r in rows)
    if pmax <= pmin:
        return [PriceBin(pmin, pmax, pmin, sum(r[2] for r in rows))]
    width = (pmax - pmin) / bins
    buckets = [0.0] * bins
    for low, high, qvol in rows:
        lo_i = max(0, min(bins - 1, int((low - pmin) / width)))
        hi_i = max(0, min(bins - 1, int((high - pmin) / width)))
        per = qvol / (hi_i - lo_i + 1)
        for i in range(lo_i, hi_i + 1):
            buckets[i] += per
    out = []
    for i, v in enumerate(buckets):
        lo = pmin + i * width
        out.append(PriceBin(low=lo, high=lo + width, center=lo + width / 2, volume=v))
    return out


def high_volume_nodes(profile: list[PriceBin], top_k: int = 6) -> list[PriceBin]:
    """Los `top_k` bins con más volumen, devueltos ordenados por precio."""
    ranked = sorted(profile, key=lambda b: b.volume, reverse=True)[:top_k]
    return sorted(ranked, key=lambda b: b.center)


def sr_zones(profile: list[PriceBin], current_price: float,
             top_k: int = 6) -> list[Zone]:
    """Convierte los nodos de alto volumen en zonas soporte/resistencia
    (según estén por debajo/encima del precio), ordenadas por cercanía."""
    hvns = high_volume_nodes(profile, top_k=top_k)
    maxv = max((b.volume for b in hvns), default=0.0) or 1.0
    zones = [
        Zone(low=b.low, high=b.high, strength=b.volume / maxv,
             kind="support" if b.center < current_price else "resistance")
        for b in hvns
    ]
    zones.sort(key=lambda z: abs(z.mid - current_price))
    return zones


def swing_levels(klines: list[list], lookback: int = 2) -> tuple[list[float], list[float]]:
    """Máximos y mínimos locales (pivotes) de highs/lows."""
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    sh, sl = [], []
    for i in range(lookback, len(klines) - lookback):
        if highs[i] == max(highs[i - lookback:i + lookback + 1]):
            sh.append(highs[i])
        if lows[i] == min(lows[i - lookback:i + lookback + 1]):
            sl.append(lows[i])
    return sh, sl


# --- 2. Liquidación: clusters reales + niveles estimados ---------------------
def cluster_liquidations(prints: Iterable[tuple], bin_pct: float = 0.005) -> list[LiqCluster]:
    """Agrupa prints de liquidación (price, qty, side) en bandas de `bin_pct`
    de ancho. `side` es el lado liquidado. Devuelto por qty descendente."""
    items = sorted((float(p), float(q), s) for p, q, s in prints if float(p) > 0)
    if not items:
        return []
    clusters: list[LiqCluster] = []
    lo = hi = items[0][0]
    total = 0.0
    sides: dict[str, float] = {}
    for price, qty, side in items:
        if price <= hi * (1 + bin_pct):
            hi = max(hi, price)
            total += qty
            sides[side] = sides.get(side, 0.0) + qty
        else:
            dom = max(sides, key=sides.get) if sides else None
            clusters.append(LiqCluster(lo, hi, total, dom))
            lo = hi = price
            total = qty
            sides = {side: qty}
    clusters.append(LiqCluster(lo, hi, total, max(sides, key=sides.get) if sides else None))
    return sorted(clusters, key=lambda c: c.qty, reverse=True)


def estimated_liq_levels(price: float, side: str,
                         leverages: tuple[int, ...] = (25, 50, 100)) -> list[float]:
    """Niveles de liquidación estimados por apalancamiento típico. Los longs se
    liquidan por debajo; los shorts por encima. Estimación, no mapa exacto."""
    return [price * (1 - 1.0 / lev) if side == "long" else price * (1 + 1.0 / lev)
            for lev in leverages]


# --- 3. Flujo / sesgo direccional --------------------------------------------
def oi_change_pct(hist_rows: list[dict]) -> Optional[float]:
    """% de cambio del interés abierto entre la 1ª y última fila de openInterestHist."""
    vals = []
    for r in hist_rows:
        try:
            vals.append(float(r["sumOpenInterest"]))
        except (KeyError, ValueError, TypeError):
            continue
    if len(vals) < 2 or vals[0] == 0:
        return None
    return (vals[-1] - vals[0]) / vals[0] * 100.0


def latest_taker_ratio(rows: list[dict]) -> Optional[float]:
    """buySellRatio de la última fila de takerlongshortRatio (>1 = compra agresiva)."""
    if not rows:
        return None
    try:
        return float(rows[-1]["buySellRatio"])
    except (KeyError, ValueError, TypeError):
        return None


def directional_bias(price_change_pct: float, oi_pct: Optional[float],
                     funding_rate: Optional[float],
                     taker_ratio: Optional[float]) -> Bias:
    """Combina precio+OI, taker ratio y funding en un sesgo [-1, 1]."""
    s = 0.0
    notes: list[str] = []
    if oi_pct is not None:
        if price_change_pct >= 0 and oi_pct > 0:
            s += 0.4
            notes.append("OI sube con el precio: continuación alcista")
        elif price_change_pct < 0 and oi_pct > 0:
            s -= 0.4
            notes.append("OI sube con precio cayendo: presión bajista")
        elif oi_pct < 0:
            notes.append("OI baja: movimiento sin convicción")
    if taker_ratio is not None:
        if taker_ratio > 1.05:
            s += 0.3
            notes.append("taker comprador dominante")
        elif taker_ratio < 0.95:
            s -= 0.3
            notes.append("taker vendedor dominante")
    if funding_rate is not None:
        if funding_rate > 0.0005:
            s -= 0.15
            notes.append("funding alto: longs sobre-extendidos (riesgo squeeze)")
        elif funding_rate < -0.0005:
            s += 0.15
            notes.append("funding negativo: shorts sobre-extendidos")
    s = max(-1.0, min(1.0, s))
    label = "long" if s > 0.15 else "short" if s < -0.15 else "neutral"
    return Bias(round(s, 3), label, notes)


# --- 4. Ajuste a tu perfil ganador -------------------------------------------
def profile_fit(aligned_with_trend: bool, entry_rsi: Optional[float],
                winner_rsi: Optional[float]) -> float:
    """0..1: cuánto se parece el setup a tus ganadoras (alineado con tendencia
    + RSI de entrada cercano al promedio de tus ganadoras)."""
    score = 0.5 if aligned_with_trend else 0.0
    if entry_rsi is not None and winner_rsi is not None:
        score += 0.5 * max(0.0, 1.0 - abs(entry_rsi - winner_rsi) / 50.0)
    return min(1.0, score)


# --- 5. Calidad: R:R neto de fees --------------------------------------------
def net_rr(entry: float, stop: float, target: float,
           fee_roundtrip: float = 0.001) -> float:
    """R:R después de fees. fee_roundtrip ~0.001 (0.1% taker ida+vuelta),
    ~0.0004 si entras maker. Resta el costo al premio y lo suma al riesgo."""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    fee = entry * fee_roundtrip
    denom = risk + fee
    if denom <= 0:
        return 0.0
    return max(0.0, (reward - fee) / denom)


# --- Dimensionado con riesgo definido ----------------------------------------
def safe_sizing(available: float, sl_frac: float, min_notional: float, *,
                risk_frac: float = 0.10, max_leverage: int = 10,
                buffer: float = 1.6, min_margin: float = 0.20) -> dict:
    """Dimensiona con riesgo definido y liquidación fuera del SL.

    margen = fracción del saldo; apalancamiento = el más alto que deje la
    liquidación (~1/lev en aislado) más lejos que el SL × buffer, y a la vez
    suficiente para el mínimo notional. Si el SL es tan ancho que el lev seguro
    no alcanza el mínimo notional, `sl_wide=True` (mejor saltar ese setup).
    """
    margin = max(min_margin, available * risk_frac)
    lev_min = max(1, math.ceil(min_notional / margin)) if margin > 0 else max_leverage
    lev_safe = int(1.0 / (sl_frac * buffer)) if sl_frac > 0 else max_leverage
    lev = max(1, min(max_leverage, lev_safe))
    sl_wide = lev < lev_min
    if sl_wide:
        lev = min(max_leverage, lev_min)
    notional = max(margin * lev, min_notional)
    return {"margin": round(margin, 4), "leverage": lev,
            "notional": round(notional, 4), "sl_wide": sl_wide}


# --- Indicadores auxiliares (para tendencia / RSI de entrada / libro) --------
def sma(values: list[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """RSI clásico (media simple de ganancias/pérdidas)."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = (gains / period) / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def trend_direction(closes: list[float], fast: int = 20, slow: int = 50) -> str:
    """'up' / 'down' / 'flat' según cruce de medias."""
    f, s = sma(closes, fast), sma(closes, slow)
    if f is None or s is None:
        return "flat"
    return "up" if f > s else "down" if f < s else "flat"


def depth_imbalance(depth: dict, levels: int = 50) -> float:
    """Desequilibrio del libro en [-1, 1]. + = más notional de compra (soporte)."""
    bids = depth.get("bids", [])[:levels]
    asks = depth.get("asks", [])[:levels]
    bid_n = sum(float(p) * float(q) for p, q in bids)
    ask_n = sum(float(p) * float(q) for p, q in asks)
    tot = bid_n + ask_n
    return (bid_n - ask_n) / tot if tot > 0 else 0.0


# --- Gates + score + ranking -------------------------------------------------
def passes_gates(rr_net: float, quote_volume: float, min_notional: float,
                 capacity: float, gates: Gates,
                 loser_pattern: bool = False) -> tuple[bool, list[str]]:
    """Filtros duros. Devuelve (pasa, motivos_de_rechazo)."""
    reasons = []
    if rr_net < gates.min_rr:
        reasons.append(f"R:R {rr_net:.2f} < {gates.min_rr}")
    if quote_volume < gates.min_quote_volume:
        reasons.append("liquidez 24h insuficiente")
    if min_notional > capacity:
        reasons.append(f"min_notional {min_notional:g} > capacidad {capacity:.1f}")
    if loser_pattern:
        reasons.append("coincide con tu patrón perdedor/liquidado")
    return (not reasons, reasons)


def composite_score(components: dict, weights: Optional[dict] = None) -> float:
    """Suma ponderada de componentes (cada uno 0..1) -> 0..100."""
    w = weights or DEFAULT_WEIGHTS
    total = sum(w.values()) or 1.0
    s = sum(w.get(k, 0.0) * max(0.0, min(1.0, components.get(k, 0.0))) for k in w)
    return round(s / total * 100.0, 1)


def rank_setups(setups: Iterable[Setup], top_n: int = 3,
                min_score: float = 0.0) -> list[Setup]:
    """Ordena por score y devuelve los mejores `top_n` (ya filtrados por gates)."""
    ok = [s for s in setups if s.score > min_score]
    ok.sort(key=lambda s: s.score, reverse=True)
    return ok[:top_n]
