"""Monitor de Binance Simple Earn (SOLO LECTURA).

Lee, con las credenciales de producción de solo-lectura (BINANCE_PROD_*):
  * Productos Flexible con su APR en tiempo real -> GET /sapi/v1/simple-earn/flexible/list
    (peso 150: se pagina y conviene cachear, no martillear).
  * Mis posiciones Flexible/Locked (APR, recompensas de ayer y acumuladas)
    -> /sapi/v1/simple-earn/flexible/position · /sapi/v1/simple-earn/locked/position
  * Resumen de cuenta Earn (total en USDT) -> GET /sapi/v1/simple-earn/account

Es la API SPOT (https://api.binance.com), distinta de futuros; se reutiliza el
cliente (firma HMAC, backoff, rate-limit) cambiando la base. NUNCA ejecuta
suscripciones/redenciones: las acciones (TRADE) son una fase aparte con permisos
y decisión explícitos.

Nota de lectura de APRs: Binance devuelve fracciones ("0.35" = 35%). Los APRs
altos suelen ser promocionales/por tramos (tierAnnualPercentageRate) y se pagan
en el propio token: si el precio del token cae más que el interés, se pierde en
USDT aunque el APR luzca enorme.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .client import BinanceFuturesClient

SPOT_BASE = "https://api.binance.com"


def make_spot_client(api_key: str, api_secret: str, *, timeout: float = 15.0,
                     ) -> BinanceFuturesClient:
    """Cliente para la API spot/sapi reutilizando firma, backoff y rate-limit."""
    cli = BinanceFuturesClient(api_key, api_secret, testnet=False, timeout=timeout)
    cli.base_url = SPOT_BASE
    return cli


@dataclass
class EarnProduct:
    product_id: str
    asset: str
    apr: float                 # fracción (0.35 = 35%) — APR base "latest"
    can_purchase: bool
    min_purchase: float
    has_bonus_tiers: bool      # APR extra por tramos/promo (ver app de Binance)


@dataclass
class EarnPosition:
    asset: str
    amount: float
    apr: float                 # fracción
    yesterday_rewards: float   # recompensa de ayer (en el asset)
    cumulative_rewards: float  # acumulado (en el asset)
    kind: str                  # "flexible" | "locked"


# --- Parsers (puros, testeables) ----------------------------------------------
def parse_products(rows) -> list[EarnProduct]:
    out = []
    for r in rows:
        try:
            out.append(EarnProduct(
                product_id=str(r.get("productId", "")),
                asset=r.get("asset", ""),
                apr=float(r.get("latestAnnualPercentageRate", 0) or 0),
                can_purchase=bool(r.get("canPurchase", False)),
                min_purchase=float(r.get("minPurchaseAmount", 0) or 0),
                has_bonus_tiers=bool(r.get("tierAnnualPercentageRate")),
            ))
        except (TypeError, ValueError):
            continue
    return out


def parse_flexible_positions(rows) -> list[EarnPosition]:
    out = []
    for r in rows:
        try:
            out.append(EarnPosition(
                asset=r.get("asset", ""),
                amount=float(r.get("totalAmount", 0) or 0),
                apr=float(r.get("latestAnnualPercentageRate", 0) or 0),
                yesterday_rewards=float(r.get("yesterdayRealTimeRewards", 0) or 0),
                cumulative_rewards=float(r.get("cumulativeTotalRewards", 0) or 0),
                kind="flexible",
            ))
        except (TypeError, ValueError):
            continue
    return out


def parse_locked_positions(rows) -> list[EarnPosition]:
    out = []
    for r in rows:
        try:
            out.append(EarnPosition(
                asset=r.get("asset", ""),
                amount=float(r.get("amount", 0) or 0),
                apr=float(r.get("apr", r.get("APY", 0)) or 0),
                yesterday_rewards=0.0,
                cumulative_rewards=float(r.get("rewardAmt", 0) or 0),
                kind="locked",
            ))
        except (TypeError, ValueError):
            continue
    return out


def top_products(products, *, limit: int = 10, only_purchasable: bool = True,
                 held_assets: Optional[set] = None) -> list[dict]:
    """Top APRs (desc). Marca con `mine=True` los assets que ya tengo en Earn."""
    held = held_assets or set()
    items = [p for p in products if (p.can_purchase or not only_purchasable)]
    items.sort(key=lambda p: p.apr, reverse=True)
    return [{"product": p, "mine": p.asset in held} for p in items[:limit]]


def summarize_positions(positions) -> dict:
    return {
        "count": len(positions),
        "assets": sorted({p.asset for p in positions}),
        "yesterday_total_by_asset": {
            p.asset: p.yesterday_rewards for p in positions if p.yesterday_rewards},
        "best_apr_held": max((p.apr for p in positions), default=0.0),
    }


# --- Acceso a la API (lectura, paginado) ---------------------------------------
async def _paged(client, path: str, *, row_key: str = "rows", size: int = 100,
                 max_pages: int = 10, **params) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        data = await client._request(
            "GET", path, {"current": page, "size": size, **params}, signed=True)
        batch = data.get(row_key, []) if isinstance(data, dict) else data
        rows.extend(batch)
        if len(batch) < size:
            break
    return rows


async def fetch_products(client, asset: Optional[str] = None) -> list[EarnProduct]:
    rows = await _paged(client, "/sapi/v1/simple-earn/flexible/list", asset=asset)
    return parse_products(rows)


async def fetch_positions(client) -> list[EarnPosition]:
    flex = await _paged(client, "/sapi/v1/simple-earn/flexible/position")
    locked = await _paged(client, "/sapi/v1/simple-earn/locked/position")
    return parse_flexible_positions(flex) + parse_locked_positions(locked)


async def fetch_account(client) -> dict:
    """Totales de Earn: {'totalAmountInUSDT': ..., 'totalAmountInBTC': ...}."""
    return await client._request("GET", "/sapi/v1/simple-earn/account", signed=True)
