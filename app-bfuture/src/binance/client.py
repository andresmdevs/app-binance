"""Cliente REST asíncrono robusto para Binance USDⓈ-M Futures.

Construido sobre aiohttp (UNA sola ClientSession reutilizable). Sobre la red cruda
añade todo lo necesario para operar sin que el exchange nos rechace o banee:

  * Firma HMAC-SHA256 con ``recvWindow`` y ``timestamp`` corregido por offset de
    reloj (evita el error -1021 INVALID_TIMESTAMP).
  * Lectura de los headers de rate-limit en CADA respuesta
    (``X-MBX-USED-WEIGHT-1m`` = peso por IP, ``X-MBX-ORDER-COUNT-*`` = órdenes por
    cuenta) para poder frenar antes de chocar.
  * Backoff que respeta ``Retry-After`` ante 429/418 y reintenta -1008 (sistema
    sobrecargado) con espera exponencial.
  * Distinción EXPLÍCITA entre un FALLO de orden y un ESTADO DESCONOCIDO
    (HTTP 503 / -1007 TIMEOUT / -1001 DISCONNECTED): en el segundo caso NO se
    reintenta a ciegas; se lanza ``BinanceUnknownStatus`` para que la capa de
    trading reconcilie (consultar la orden por su ``clientOrderId``) y no duplique.

Por defecto apunta a **TESTNET** (seguro para desarrollo). Cambia con
``BINANCE_TESTNET=false`` o ``testnet=False`` en el constructor.

Doc oficial: https://developers.binance.com/docs/derivatives/usds-margined-futures/
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

# --- Endpoints base -----------------------------------------------------------
MAINNET_REST = "https://fapi.binance.com"
TESTNET_REST = "https://testnet.binancefuture.com"
# WebSocket (mercado + user data). Testnet verificado empíricamente.
MAINNET_WS = "wss://fstream.binance.com"
TESTNET_WS = "wss://stream.binancefuture.com"

# Códigos de error de Binance que tienen tratamiento especial.
CODE_SERVER_BUSY = -1008       # sistema sobrecargado -> backoff y reintento
CODE_TIMEOUT = -1007           # estado de ejecución DESCONOCIDO
CODE_DISCONNECTED = -1001      # estado de ejecución DESCONOCIDO
CODE_INVALID_TIMESTAMP = -1021  # reloj fuera de la ventana -> resync


# --- Jerarquía de excepciones -------------------------------------------------
class BinanceError(Exception):
    """Base de todos los errores de esta capa."""


class BinanceAPIError(BinanceError):
    """Error de negocio devuelto por Binance (HTTP 4xx con code/msg).

    Es un fallo determinista del lado cliente (parámetro inválido, margen
    insuficiente, filtro incumplido, etc.). NO se debe reintentar a ciegas.
    """

    def __init__(self, status: int, code: Optional[int], msg: str):
        self.status = status
        self.code = code
        self.msg = msg
        super().__init__(f"HTTP {status} code={code}: {msg}")


class BinanceRateLimit(BinanceError):
    """429 (límite excedido) o 418 (IP baneada). ``retry_after`` en segundos."""

    def __init__(self, status: int, retry_after: Optional[float], msg: str = ""):
        self.status = status
        self.retry_after = retry_after
        banned = status == 418
        super().__init__(
            f"HTTP {status} ({'IP BANEADA' if banned else 'rate limit'}), "
            f"reintentar en {retry_after}s. {msg}"
        )


class BinanceServerBusy(BinanceError):
    """-1008: el motor está sobrecargado. Transitorio; reintentar más tarde."""


class BinanceUnknownStatus(BinanceError):
    """ESTADO DE EJECUCIÓN DESCONOCIDO (HTTP 503 / -1007 / -1001 / red caída
    en una escritura).

    La orden PUEDE haberse ejecutado o no. Quien recibe esta excepción debe
    reconciliar consultando el estado real de la orden antes de reintentar.
    """


class BinanceFuturesClient:
    """Cliente REST async para USDⓈ-M Futures.

    Uso típico (gestiona la sesión por ti)::

        async with BinanceFuturesClient(api_key, secret, testnet=True) as cli:
            await cli.sync_time()
            info = await cli.exchange_info()
            pos = await cli.position_risk()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        *,
        testnet: bool = True,
        recv_window: int = 5000,
        timeout: float = 10.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.testnet = testnet
        self.base_url = TESTNET_REST if testnet else MAINNET_REST
        self.ws_base = TESTNET_WS if testnet else MAINNET_WS
        # recvWindow recomendado <= 5000 ms por seguridad (Binance permite hasta 60000).
        self.recv_window = min(int(recv_window), 60000)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries

        self._session: Optional[aiohttp.ClientSession] = None
        self.time_offset = 0  # ms: server_time - local_time, calibrado por sync_time()
        # Últimos valores de rate-limit leídos de los headers (para la UI/freno).
        self.rate_limits: dict[str, int] = {}

    # --- Ciclo de vida de la sesión ------------------------------------------
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "BinanceFuturesClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # --- Firma y utilidades ---------------------------------------------------
    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self.time_offset

    def _require_keys(self) -> None:
        if not self.api_key or not self.secret_key:
            raise BinanceError(
                "Se requieren BINANCE_API_KEY y BINANCE_API_SECRET para endpoints firmados."
            )

    def _sign(self, params: dict) -> str:
        """Devuelve el query string firmado (con ``signature`` al final)."""
        query = urlencode(params, doseq=True)
        signature = hmac.new(
            self.secret_key.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _capture_rate_limits(self, headers) -> None:
        for k, v in headers.items():
            ku = k.upper()
            if ku.startswith("X-MBX-USED-WEIGHT") or ku.startswith("X-MBX-ORDER-COUNT"):
                try:
                    self.rate_limits[ku] = int(v)
                except ValueError:
                    pass

    @staticmethod
    def _parse_retry_after(headers) -> Optional[float]:
        ra = headers.get("Retry-After")
        if ra is None:
            return None
        try:
            return float(ra)
        except ValueError:
            return None

    def _backoff_delay(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return retry_after
        # exponencial: 1s, 2s, 4s... con un pequeño suelo.
        return min(2.0 ** attempt, 30.0)

    # --- Núcleo de peticiones -------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        *,
        signed: bool = False,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        is_write = method.upper() in ("POST", "DELETE", "PUT")
        if signed:
            self._require_keys()
        session = await self._ensure_session()
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}

        attempt = 0
        while True:
            # Firma fresca en cada intento (timestamp nuevo dentro de recvWindow).
            if signed:
                p = dict(params)
                p["timestamp"] = self._timestamp()
                p["recvWindow"] = self.recv_window
                query = self._sign(p)
            else:
                query = urlencode(params, doseq=True)
            url = f"{self.base_url}{path}" + (f"?{query}" if query else "")

            try:
                async with session.request(method, url, headers=headers) as resp:
                    self._capture_rate_limits(resp.headers)
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        data = {"_raw": text}

                    if resp.status == 200:
                        return data

                    code = data.get("code") if isinstance(data, dict) else None
                    msg = data.get("msg") if isinstance(data, dict) else text
                    retry_after = self._parse_retry_after(resp.headers)

                    # 429/418: respetar Retry-After y reintentar (o abortar).
                    if resp.status in (429, 418):
                        if attempt < self.max_retries and resp.status == 429:
                            await asyncio.sleep(self._backoff_delay(attempt, retry_after))
                            attempt += 1
                            continue
                        raise BinanceRateLimit(resp.status, retry_after, msg or "")

                    # ESTADO DESCONOCIDO: no reintentar a ciegas.
                    if resp.status == 503 or code in (CODE_TIMEOUT, CODE_DISCONNECTED):
                        raise BinanceUnknownStatus(
                            f"Estado de ejecución desconocido (HTTP {resp.status}, code={code}): {msg}"
                        )

                    # -1008: sobrecarga -> backoff y reintento.
                    if code == CODE_SERVER_BUSY:
                        if attempt < self.max_retries:
                            await asyncio.sleep(self._backoff_delay(attempt, retry_after))
                            attempt += 1
                            continue
                        raise BinanceServerBusy(msg or "Servidor sobrecargado (-1008)")

                    # -1021: reloj desincronizado -> resync una vez y reintentar.
                    if code == CODE_INVALID_TIMESTAMP and attempt == 0 and signed:
                        await self.sync_time()
                        attempt += 1
                        continue

                    # Resto: error determinista del cliente.
                    raise BinanceAPIError(resp.status, code, msg or text)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Fallo de red. En una ESCRITURA el estado es desconocido.
                if attempt < self.max_retries:
                    await asyncio.sleep(self._backoff_delay(attempt, None))
                    attempt += 1
                    continue
                if is_write:
                    raise BinanceUnknownStatus(
                        f"Red caída durante escritura; estado desconocido: {e}"
                    ) from e
                raise BinanceError(f"Error de red: {e}") from e

    # --- Endpoints públicos (NONE) -------------------------------------------
    async def ping(self) -> dict:
        return await self._request("GET", "/fapi/v1/ping")

    async def server_time(self) -> int:
        data = await self._request("GET", "/fapi/v1/time")
        return int(data["serverTime"])

    async def sync_time(self) -> int:
        """Calibra ``time_offset`` para que todas las firmas usen la hora del servidor."""
        local = int(time.time() * 1000)
        server = await self.server_time()
        self.time_offset = server - local
        return self.time_offset

    async def exchange_info(self) -> dict:
        return await self._request("GET", "/fapi/v1/exchangeInfo")

    async def klines(self, symbol: str, interval: str, limit: int = 500, **kw) -> list:
        params = {"symbol": symbol, "interval": interval, "limit": limit, **kw}
        return await self._request("GET", "/fapi/v1/klines", params)

    async def mark_price(self, symbol: Optional[str] = None) -> Any:
        return await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    async def ticker_24h(self, symbol: Optional[str] = None) -> Any:
        return await self._request("GET", "/fapi/v1/ticker/24hr", {"symbol": symbol})

    async def book_ticker(self, symbol: str) -> dict:
        """Mejor bid/ask actuales (para colocar entradas maker)."""
        return await self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    # --- User Data Stream (USER_STREAM: solo header API-key, sin firma) -------
    async def create_listen_key(self) -> str:
        data = await self._request("POST", "/fapi/v1/listenKey")
        return data["listenKey"]

    async def keepalive_listen_key(self) -> dict:
        return await self._request("PUT", "/fapi/v1/listenKey")

    async def close_listen_key(self) -> dict:
        return await self._request("DELETE", "/fapi/v1/listenKey")

    # --- Endpoints firmados de LECTURA (USER_DATA) ---------------------------
    async def position_risk(self, symbol: Optional[str] = None) -> list:
        """Posiciones (v3: devuelve mantenidas y en apertura)."""
        return await self._request(
            "GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True
        )

    async def account(self) -> dict:
        return await self._request("GET", "/fapi/v3/account", signed=True)

    async def balance(self) -> list:
        return await self._request("GET", "/fapi/v2/balance", signed=True)

    async def open_orders(self, symbol: Optional[str] = None) -> list:
        # OJO: sin `symbol` el peso es 40 (vs 1 con symbol).
        return await self._request(
            "GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True
        )

    async def user_trades(self, symbol: str, limit: int = 500, **kw) -> list:
        params = {"symbol": symbol, "limit": limit, **kw}
        return await self._request("GET", "/fapi/v1/userTrades", params, signed=True)

    async def income(self, **kw) -> list:
        return await self._request("GET", "/fapi/v1/income", kw, signed=True)

    async def force_orders(self, symbol: Optional[str] = None,
                           auto_close_type: Optional[str] = None, **kw) -> list:
        """Órdenes forzadas del usuario (LIQUIDATION/ADL). Solo últimos 90 días.
        Peso 20 con symbol, 50 sin symbol.
        """
        params = {"symbol": symbol, "autoCloseType": auto_close_type, **kw}
        return await self._request("GET", "/fapi/v1/forceOrders", params, signed=True)
