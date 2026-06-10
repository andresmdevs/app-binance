"""Smoke test SEGURO del módulo de trading contra TESTNET.

Qué hace (todo en testnet, dinero ficticio, y se limpia solo):
  1. Lee el modo de posición (one-way / hedge).
  2. Coloca una orden LIMIT de COMPRA muy por DEBAJO del mercado (~40%): queda
     en reposo y NO se ejecuta, así que NO abre ninguna posición.
  3. Comprueba que aparece en openOrders y la consulta por su clientOrderId.
  4. La CANCELA. Estado final: sin órdenes nuevas, sin posiciones.

No abre posiciones ni mueve nada irreversible. Aun así, ABORTA si detecta que el
cliente NO está en testnet (protección anti dinero real).

Uso:
    PYTHONPATH=src uv run python -m binance.tradedemo
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

from .client import BinanceError, BinanceFuturesClient
from .filters import FilterCache
from .trade import LIMIT, FuturesTrader

SYMBOL = "BTCUSDT"


def _load_keys() -> tuple[str | None, str | None]:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except Exception:
        pass
    return os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET")


async def run() -> None:
    key, sec = _load_keys()
    if not key or not sec:
        print("Faltan claves en .env -> aborto.")
        return

    # Forzamos testnet: este script NUNCA debe tocar producción.
    async with BinanceFuturesClient(key, sec, testnet=True) as cli:
        if not cli.testnet:
            print("ABORTO: el cliente no está en testnet.")
            return
        await cli.sync_time()
        filters = FilterCache(cli)
        await filters.refresh()
        trader = FuturesTrader(cli, filters)

        hedge = await trader.get_position_mode()
        print(f"Modo de posición: {'HEDGE' if hedge else 'ONE-WAY'}")

        # Precio actual y objetivo ~40% por debajo (no se ejecutará).
        mp = await cli.mark_price(SYMBOL)
        mark = Decimal(str((mp[0] if isinstance(mp, list) else mp)["markPrice"]))
        sf = filters.get(SYMBOL)
        target = mark * Decimal("0.60")
        # Cantidad mínima que supere el notional mínimo a ese precio.
        qty = (sf.min_notional / target) * Decimal("1.2")
        qty = max(qty, sf.min_qty)
        print(f"Mark={mark} · limit objetivo={sf.format_price(target)} · qty={sf.format_qty(qty)}")

        print("\n1) Colocando LIMIT BUY de reposo (no se ejecuta)...")
        order = await trader.limit(SYMBOL, "BUY", quantity=qty, price=target)
        oid = order.get("orderId")
        coid = order.get("clientOrderId")
        print(f"   -> orderId={oid} status={order.get('status')} "
              f"precio={order.get('price')} qty={order.get('origQty')}")

        print("2) Consultando la orden por clientOrderId...")
        q = await trader.query_order(SYMBOL, client_order_id=coid)
        print(f"   -> status={q.get('status')} type={q.get('type')} side={q.get('side')}")

        opens = await cli.open_orders(SYMBOL)
        print(f"   openOrders({SYMBOL})={len(opens)}")

        print("3) Cancelando...")
        c = await trader.cancel_order(SYMBOL, order_id=oid)
        print(f"   -> status={c.get('status')}")

        opens_after = await cli.open_orders(SYMBOL)
        positions = [p for p in await cli.position_risk(SYMBOL)
                     if float(p.get("positionAmt", 0)) != 0]
        print(f"\nEstado final: openOrders={len(opens_after)} · posiciones={len(positions)}")
        print("SMOKE TEST OK" if not opens_after and not positions
              else "REVISAR: quedó estado residual")


def main() -> None:
    try:
        asyncio.run(run())
    except BinanceError as e:
        print(f"Error Binance: {e}")


if __name__ == "__main__":
    main()
