"""WebSockets de Binance USDⓈ-M Futures: market streams + user data stream.

Reduce la presión sobre el límite REST: en vez de hacer polling, recibimos
actualizaciones por push. Usa su PROPIA ClientSession aiohttp (sin timeout total,
porque las conexiones WS son de larga duración) y reconecta con backoff exponencial.

  MarketStream: suscribe N streams de mercado combinados (kline, markPrice, ...).
  UserStream:   crea/renueva el listenKey y entrega ACCOUNT_UPDATE / ORDER_TRADE_UPDATE.
                Renueva la key cada 30 min (válida 60) y la recrea si expira.

Los callbacks pueden ser síncronos o async; se await-ean si hace falta.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Iterable, Optional

import aiohttp

from .client import BinanceFuturesClient

Handler = Callable[[dict], object]
StatusHandler = Callable[[str], object]


async def _maybe_await(result) -> None:
    if asyncio.iscoroutine(result):
        await result


class _BaseStream:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # total=None: las WS viven mucho; el heartbeat detecta caídas.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15)
            )
        return self._session

    def start(self) -> None:
        """Arranca el bucle. Debe llamarse dentro de un event loop en marcha."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _run(self) -> None:  # pragma: no cover
        raise NotImplementedError


class MarketStream(_BaseStream):
    """Suscribe streams de mercado combinados en una sola conexión."""

    def __init__(
        self,
        ws_base: str,
        streams: Iterable[str],
        on_message: Handler,
        *,
        on_status: Optional[StatusHandler] = None,
    ) -> None:
        super().__init__()
        self.ws_base = ws_base
        self.streams = list(streams)
        self.on_message = on_message
        self.on_status = on_status

    async def _run(self) -> None:
        backoff = 1
        url = f"{self.ws_base}/stream?streams={'/'.join(self.streams)}"
        while self._running:
            try:
                session = await self._ensure_session()
                async with session.ws_connect(url, heartbeat=30) as ws:
                    backoff = 1
                    if self.on_status:
                        await _maybe_await(self.on_status("conectado"))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            # /stream envuelve en {"stream":..., "data":...}
                            await _maybe_await(self.on_message(payload.get("data", payload)))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.on_status:
                    await _maybe_await(self.on_status(f"reconectando: {e}"))
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


class UserStream(_BaseStream):
    """Stream de datos de usuario (posiciones/órdenes) vía listenKey."""

    def __init__(
        self,
        client: BinanceFuturesClient,
        on_event: Handler,
        *,
        on_status: Optional[StatusHandler] = None,
        keepalive_seconds: int = 1800,
    ) -> None:
        super().__init__()
        self.client = client
        self.on_event = on_event
        self.on_status = on_status
        self.keepalive_seconds = keepalive_seconds
        self._keepalive_task: Optional[asyncio.Task] = None

    async def _keepalive_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.keepalive_seconds)
            try:
                await self.client.keepalive_listen_key()
            except Exception:
                pass  # si falla, _run reconectará y recreará la key

    async def _run(self) -> None:
        backoff = 1
        while self._running:
            try:
                listen_key = await self.client.create_listen_key()
                url = f"{self.client.ws_base}/ws/{listen_key}"
                session = await self._ensure_session()
                async with session.ws_connect(url, heartbeat=30) as ws:
                    backoff = 1
                    if self.on_status:
                        await _maybe_await(self.on_status("user stream conectado"))
                    self._keepalive_task = asyncio.create_task(self._keepalive_loop())
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("e") == "listenKeyExpired":
                                break  # forzar recreación de la key
                            await _maybe_await(self.on_event(data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.on_status:
                    await _maybe_await(self.on_status(f"user stream reconectando: {e}"))
            finally:
                if self._keepalive_task:
                    self._keepalive_task.cancel()
                    self._keepalive_task = None
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self) -> None:
        await super().stop()
        try:
            await self.client.close_listen_key()
        except Exception:
            pass
