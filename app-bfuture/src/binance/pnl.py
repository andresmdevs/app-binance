"""PnL histórico realizado y filtros (USDⓈ-M Futures).

Fuente: GET /fapi/v1/income con incomeType=REALIZED_PNL. Cada fila es un evento de
PnL realizado (al cerrar/reducir una posición), lo que encaja perfecto con "filtrar
operaciones con PNL positivo". Para el resultado NETO se pueden sumar también
COMMISSION y FUNDING_FEE de la misma ventana (ver ``net_summary``).

El endpoint income devuelve como mucho 1000 filas por llamada y limita la ventana
temporal, así que paginamos en tramos de 7 días hacia atrás.

La lógica de normalización/filtro/resumen es pura (sin red) y por tanto testeable
offline; solo ``fetch_*`` toca la API.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

_WEEK_MS = 7 * 24 * 60 * 60 * 1000


@dataclass
class PnlEvent:
    """Un evento de PnL realizado (una operación cerrada/reducida)."""

    symbol: str
    time: datetime  # UTC
    pnl: float      # PnL realizado bruto (sin comisiones/funding)
    asset: str = "USDT"
    trade_id: str = ""


@dataclass
class PnlSummary:
    count: int
    wins: int
    losses: int
    total: float
    gross_profit: float
    gross_loss: float  # valor positivo (suma de pérdidas en magnitud)
    win_rate: float    # 0..1
    profit_factor: float  # gross_profit / gross_loss (inf si no hay pérdidas)
    best: float
    worst: float


# --- Normalización ------------------------------------------------------------
def income_rows_to_events(rows: Iterable[dict]) -> list[PnlEvent]:
    """Convierte filas crudas de /income (REALIZED_PNL) en PnlEvent."""
    events: list[PnlEvent] = []
    for r in rows:
        if r.get("incomeType") not in (None, "REALIZED_PNL"):
            continue
        events.append(
            PnlEvent(
                symbol=r.get("symbol", ""),
                time=datetime.fromtimestamp(int(r["time"]) / 1000, tz=timezone.utc),
                pnl=float(r.get("income", 0.0)),
                asset=r.get("asset", "USDT"),
                trade_id=str(r.get("tradeId") or ""),
            )
        )
    return events


# --- Filtros y resumen (puros) ------------------------------------------------
def filter_events(
    events: Iterable[PnlEvent],
    *,
    symbol: Optional[str] = None,
    only_positive: bool = False,
    min_pnl: Optional[float] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> list[PnlEvent]:
    out = []
    for e in events:
        if symbol and e.symbol != symbol:
            continue
        if only_positive and e.pnl <= 0:
            continue
        if min_pnl is not None and e.pnl < min_pnl:
            continue
        if start and e.time < start:
            continue
        if end and e.time > end:
            continue
        out.append(e)
    return out


def summarize(events: Iterable[PnlEvent]) -> PnlSummary:
    events = list(events)
    pnls = [e.pnl for e in events]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)  # magnitud positiva
    n = len(pnls)
    return PnlSummary(
        count=n,
        wins=len(wins),
        losses=len(losses),
        total=sum(pnls),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        win_rate=(len(wins) / n) if n else 0.0,
        profit_factor=(gross_profit / gross_loss) if gross_loss else float("inf"),
        best=max(pnls) if pnls else 0.0,
        worst=min(pnls) if pnls else 0.0,
    )


# --- Acceso a la API (chunked) ------------------------------------------------
async def _fetch_income(
    client, income_type: str, *, symbol: Optional[str], start_ms: int, end_ms: int
) -> list[dict]:
    """Pagina /income en tramos de 7 días para esquivar el límite de ventana/filas."""
    rows: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        chunk_end = min(cur + _WEEK_MS, end_ms)
        params = {
            "incomeType": income_type,
            "startTime": cur,
            "endTime": chunk_end,
            "limit": 1000,
        }
        if symbol:
            params["symbol"] = symbol
        batch = await client.income(**params)
        rows.extend(batch)
        # Si el tramo se llenó (1000), avanzamos justo tras la última fila.
        if len(batch) >= 1000:
            cur = int(batch[-1]["time"]) + 1
        else:
            cur = chunk_end
    return rows


async def fetch_realized_pnl(
    client, *, symbol: Optional[str] = None, days: int = 30,
    start_ms: Optional[int] = None, end_ms: Optional[int] = None,
) -> list[PnlEvent]:
    """Trae los eventos de PnL realizado de la ventana indicada (UTC)."""
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = start_ms or (end_ms - days * 24 * 60 * 60 * 1000)
    rows = await _fetch_income(
        client, "REALIZED_PNL", symbol=symbol, start_ms=start_ms, end_ms=end_ms
    )
    events = income_rows_to_events(rows)
    events.sort(key=lambda e: e.time, reverse=True)
    return events


async def net_summary(
    client, *, symbol: Optional[str] = None, days: int = 30,
) -> dict:
    """Resumen NETO: PnL realizado menos comisiones y funding de la ventana."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    realized = sum(
        e.pnl for e in await fetch_realized_pnl(
            client, symbol=symbol, start_ms=start_ms, end_ms=end_ms
        )
    )
    commission = sum(
        float(r.get("income", 0)) for r in await _fetch_income(
            client, "COMMISSION", symbol=symbol, start_ms=start_ms, end_ms=end_ms
        )
    )
    funding = sum(
        float(r.get("income", 0)) for r in await _fetch_income(
            client, "FUNDING_FEE", symbol=symbol, start_ms=start_ms, end_ms=end_ms
        )
    )
    return {
        "realized_pnl": realized,
        "commission": commission,  # normalmente negativo
        "funding": funding,        # puede ser +/-
        "net": realized + commission + funding,
    }
