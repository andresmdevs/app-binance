"""Motor de ejecución AUTÓNOMO (estilo modo autónomo del bot de Exnova).

Ciclo por vela (modo "vela", 1m por defecto)::

    T-4s   ANALIZA: baja las últimas velas de cada símbolo, cada indicador vota
           (`autosignal.decide`), aplica los gates (señal mínima, volatilidad vs
           comisiones, riesgo/kill-switch) y deja el símbolo listo (margen
           aislado + apalancamiento ya fijados).
    T+0.3s EJECUTA al abrir la vela: MARKET + bracket TP/SL (órdenes
           condicionales que viven en el exchange: protegen aunque la app muera).
    ...    SUPERVISA: break-even/trailing opcional; cierra por tiempo ~6 s antes
           del siguiente análisis, para poder operar una vez por vela.
    fin    PnL NETO real (userTrades: realizado − comisiones) -> estadísticas,
           racha de pérdidas y diario JSONL con los votos de cada orden.

Modo "radar": el mismo ejecutor, pero las señales salen del motor de señales
existente (`scanner.scan_market`, datos públicos de mainnet) y se ejecutan con
sus % de stop/objetivo sobre el precio de la cuenta (como "Preparar orden" +
"Confirmar", pero sin intervención).

Seguridad (no negociable):
  * SOLO TESTNET: se niega a arrancar contra producción.
  * SIN martingala: tamaño fijo por orden (margen × apalancamiento), con el
    apalancamiento recortado para que la liquidación quede más lejos que el SL.
  * `RiskManager.check_open` antes de CADA orden; el kill-switch diario DETIENE
    el motor (no solo salta la señal).
  * Pausa por racha de pérdidas, tope de órdenes por sesión, una posición por
    símbolo y nunca toca un símbolo con una posición que no es suya.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from . import market as market_mod
from . import scanner
from .autosignal import KLINES_LIMIT, ShadowBoard, SignalConfig, decide
from .client import BinanceAPIError, BinanceError
from .risk import KillSwitchError, RiskError
from .scalp import ScalpConfig, close_scalp, manage_trailing_stop, open_scalp

# Intervalos alineados a múltiplos exactos desde epoch (las velas de Binance abren ahí).
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000,
}
LEV_SAFETY_BUFFER = 1.6          # liquidación (~1/lev en aislado) > SL × este buffer
SYNC_EVERY_MS = 30 * 60_000      # re-sincroniza el reloj con el servidor
MAX_TICK_ERRORS = 5              # análisis fallidos seguidos -> se detiene
MAX_ENTRY_FAILURES = 3           # entradas fallidas seguidas -> se detiene


class EngineError(Exception):
    """Configuración inválida o entorno no permitido (p.ej. producción)."""


@dataclass
class EngineConfig:
    symbols: list = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    mode: str = "candle"               # "candle" (votos por vela) | "radar"
    interval: str = "1m"
    lead_s: float = 4.0                # analiza N s ANTES de que abra la vela
    entry_delay_s: float = 0.3         # dispara N s DESPUÉS de que abra
    max_late_s: float = 2.0            # si el análisis llega más tarde, salta la vela
    margin_usdt: Decimal = Decimal("5")
    leverage: int = 5                  # tope; se recorta si el SL lo exige
    hold_candles: int = 1              # duración máx de cada orden (velas)
    exit_margin_s: float = 2.0         # cierra N s antes del siguiente análisis
    entry_type: str = "MARKET"         # "MARKET" (taker) | "MAKER" (post-only)
    maker_timeout_s: int = 10
    trailing: bool = False             # break-even + trailing del stop
    trail_pct: float = 0.003
    poll_s: float = 2.0                # sondeo de la posición mientras vive
    max_concurrent: int = 2            # posiciones del motor a la vez
    max_trades: int = 50               # tope de órdenes por sesión
    max_consecutive_losses: int = 4    # pausa de seguridad (lo contrario a martingala)
    cooldown_candles: int = 0          # velas sin re-entrar al mismo símbolo
    signal: SignalConfig = field(default_factory=SignalConfig)
    # --- modo radar ---
    radar_universe: int = 20           # top-N líquidos (si `symbols` está vacío)
    radar_min_score: float = 55.0
    radar_hold_min: int = 60


def radar_config(**kw) -> EngineConfig:
    """Valores por defecto razonables para el modo radar (estructura de 15m)."""
    base = dict(mode="radar", symbols=[], interval="5m", lead_s=25.0, max_late_s=60.0,
                trailing=True, cooldown_candles=3)
    base.update(kw)
    return EngineConfig(**base)


@dataclass
class Signal:
    symbol: str
    side: str                  # "BUY" | "SELL"
    tp_pct: float              # fracción de precio
    sl_pct: float
    exit_at_ms: int            # hora (servidor) del cierre por tiempo
    score: float
    reason: str
    source: str                # "vela" | "radar"
    votes: dict = field(default_factory=dict)
    price: float = 0.0
    leverage: int = 0
    notional: Decimal = Decimal("0")


@dataclass
class TradeRecord:
    symbol: str
    side: str
    source: str
    score: float
    reason: str
    votes: dict
    leverage: int
    notional: Decimal
    tp_pct: float
    sl_pct: float
    exit_at_ms: int
    opened_ms: int
    qty: str = ""
    entry: float = 0.0
    tp_trigger: Optional[float] = None
    sl_trigger: Optional[float] = None
    bracket_ok: bool = True
    closed_ms: Optional[int] = None
    exit_reason: str = ""
    net: Optional[float] = None
    commission: float = 0.0


@dataclass
class EngineStats:
    opened: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    unknown: int = 0           # cerradas sin poder leer su PnL
    failed: int = 0            # entradas que no se pudieron abrir
    net: float = 0.0
    commission: float = 0.0
    consec_losses: int = 0
    max_consec_losses: int = 0

    def record(self, net: Optional[float], commission: float = 0.0) -> None:
        self.closed += 1
        if net is None:
            self.unknown += 1
            return
        self.net += net
        self.commission += commission
        if net > 0:
            self.wins += 1
            self.consec_losses = 0
        else:
            self.losses += 1
            self.consec_losses += 1
            self.max_consec_losses = max(self.max_consec_losses, self.consec_losses)

    @property
    def win_rate(self) -> Optional[float]:
        n = self.wins + self.losses
        return self.wins / n if n else None


def next_tick(now_ms: int, interval_ms: int, lead_ms: int) -> int:
    """Próxima apertura de vela cuyo momento de análisis (apertura − lead) no pasó."""
    boundary = (now_ms // interval_ms + 1) * interval_ms
    if boundary - lead_ms < now_ms:
        boundary += interval_ms
    return boundary


def safe_leverage(sl_pct: float, cap: int, buffer: float = LEV_SAFETY_BUFFER) -> int:
    """El apalancamiento más alto (≤ cap) que deja la liquidación más lejos que el SL."""
    if sl_pct <= 0:
        return max(1, int(cap))
    return max(1, min(int(cap), int(1.0 / (sl_pct * buffer))))


def _side_label(side: str) -> str:
    return "LONG" if side == "BUY" else "SHORT"


def _hhmmss(ms: Optional[int]) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%H:%M:%S")


class AutoEngine:
    """Motor autónomo. Uso::

        eng = AutoEngine(client, trader, risk, EngineConfig(symbols=["SOLUSDT"]))
        eng.start()            # dentro de un event loop en marcha
        ...
        await eng.stop()       # deja de abrir; las posiciones vivas siguen supervisadas
    """

    def __init__(self, client, trader, risk, cfg: Optional[EngineConfig] = None, *,
                 scan_client=None, journal=None,
                 on_event: Optional[Callable[[dict], None]] = None,
                 clock: Optional[Callable[[], int]] = None, sleep=None,
                 allow_mainnet: bool = False):
        self.client = client
        self.trader = trader
        self.filters = trader.filters
        self.risk = risk
        self.cfg = cfg or EngineConfig()
        self.scan_client = scan_client     # cliente PÚBLICO de mainnet (modo radar)
        self.journal = journal             # objeto con .record(action, request, ...)
        self.on_event = on_event           # callback SÍNCRONO (UI/CLI)
        self._clock_fn = clock
        self._sleep = sleep or asyncio.sleep
        self.allow_mainnet = allow_mainnet

        self.stats = EngineStats()
        self.shadow = ShadowBoard()
        self.open_trades: dict[str, TradeRecord] = {}
        self.closed_trades: deque = deque(maxlen=200)
        self.events: deque = deque(maxlen=300)
        self.running = False
        self.paused_reason: Optional[str] = None
        self.next_tick_ms: Optional[int] = None

        self._task: Optional[asyncio.Task] = None
        self._trade_tasks: set = set()
        self._configured: dict[str, int] = {}     # símbolo -> apalancamiento ya fijado
        self._cooldown_until: dict[str, int] = {}
        self._last_sync_ms = 0
        self._entry_failures = 0

    # --- Utilidades -------------------------------------------------------------
    def _clock(self) -> int:
        """Hora del SERVIDOR en ms (las velas se alinean a la hora de Binance)."""
        if self._clock_fn is not None:
            return int(self._clock_fn())
        return int(time.time() * 1000) + int(getattr(self.client, "time_offset", 0) or 0)

    def _emit(self, kind: str, msg: str, **data) -> None:
        evt = {"ts": self._clock(), "kind": kind, "msg": msg, **data}
        self.events.append(evt)
        if self.on_event is not None:
            try:
                self.on_event(evt)
            except Exception:  # noqa: BLE001 - la UI nunca debe tumbar el motor
                pass

    def _journal(self, action: str, payload: dict) -> None:
        if self.journal is not None:
            try:
                self.journal.record(action, payload)
            except Exception:  # noqa: BLE001
                pass

    def _halt(self, reason: str) -> None:
        """Detiene la apertura de órdenes (las posiciones vivas siguen supervisadas)."""
        if self.running:
            self.running = False
            self.paused_reason = reason
            self._emit("halt", f"Motor detenido: {reason}")
            self._journal("engine.halt", {"reason": reason, "stats": asdict(self.stats)})

    async def _sleep_until(self, target_ms: int) -> None:
        while self.running:
            remaining = (target_ms - self._clock()) / 1000.0
            if remaining <= 0:
                return
            await self._sleep(min(remaining, 60.0))

    async def _maybe_sync(self) -> None:
        if self._clock() - self._last_sync_ms < SYNC_EVERY_MS:
            return
        try:
            await self.client.sync_time()
        except BinanceError as e:
            self._emit("error", f"no pude sincronizar el reloj: {e}")
        self._last_sync_ms = self._clock()

    def describe(self) -> str:
        c = self.cfg
        lev = f"margen {c.margin_usdt} USDT × ≤{c.leverage}x"
        if c.mode == "radar":
            uni = ", ".join(c.symbols) if c.symbols else f"top {c.radar_universe} líquidos"
            return (f"radar cada {c.interval} · {uni} · score ≥ {c.radar_min_score:g} · "
                    f"{lev} · máx {c.radar_hold_min} min")
        return (f"vela {c.interval} · {', '.join(c.symbols)} · analiza {c.lead_s:g}s antes · "
                f"votos ≥ ±{c.signal.min_score} · {lev} · {c.hold_candles} vela(s)")

    # --- Ciclo de vida ----------------------------------------------------------
    def _validate(self) -> None:
        if not getattr(self.client, "testnet", False) and not self.allow_mainnet:
            raise EngineError("El motor autónomo solo opera en TESTNET "
                              "(BINANCE_TESTNET=true). Producción está bloqueada.")
        c = self.cfg
        if c.mode not in ("candle", "radar"):
            raise EngineError(f"Modo desconocido: {c.mode}")
        if c.interval not in INTERVAL_MS:
            raise EngineError(f"Intervalo no soportado: {c.interval} "
                              f"(usa {', '.join(INTERVAL_MS)})")
        iv_s = INTERVAL_MS[c.interval] / 1000
        if not 0 < c.lead_s < iv_s:
            raise EngineError(f"La anticipación ({c.lead_s:g}s) debe estar entre 0 y "
                              f"la duración de la vela ({iv_s:g}s).")
        if c.margin_usdt <= 0 or c.leverage < 1:
            raise EngineError("Margen y apalancamiento deben ser positivos.")
        unknown = [s for s in c.symbols if s not in self.filters]
        if unknown:
            raise EngineError(f"Símbolos no disponibles en este exchange: {', '.join(unknown)}")
        if c.mode == "candle":
            if not c.symbols:
                raise EngineError("Indica al menos un símbolo.")
            if c.hold_candles < 1 or c.hold_candles * iv_s - c.lead_s - c.exit_margin_s < 5:
                raise EngineError("La duración por orden es demasiado corta para el intervalo.")
        elif self.scan_client is None:
            raise EngineError("El modo radar necesita el cliente de datos de mainnet.")

    def start(self) -> None:
        """Arranca el bucle. Debe llamarse dentro de un event loop en marcha."""
        if self._task is not None and not self._task.done():
            return
        self._validate()
        self.running = True
        self.paused_reason = None
        self._entry_failures = 0
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, close_positions: bool = False,
                   reason: str = "detenido por el usuario") -> None:
        """Deja de abrir órdenes. Con `close_positions`, cierra a mercado las
        posiciones del motor (sus tareas lo detectan y registran el PnL)."""
        if self.running:
            self.paused_reason = reason
        self.running = False
        t = self._task
        if t is not None and not t.done() and t is not asyncio.current_task():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if close_positions:
            for sym in list(self.open_trades):
                await self._safety_close(sym, label="cerrada al detener el motor")

    async def join(self) -> None:
        """Espera a que el bucle y todas las órdenes en curso terminen."""
        while True:
            pending = [t for t in (self._task, *self._trade_tasks) if t and not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def summary(self) -> dict:
        s = self.stats
        return {
            "running": self.running,
            "paused_reason": self.paused_reason,
            "next_tick_ms": self.next_tick_ms,
            "stats": {**asdict(s), "win_rate": s.win_rate},
            "open": [asdict(r) for r in self.open_trades.values()],
            "shadow": self.shadow.ranking(),
        }

    # --- Bucle principal --------------------------------------------------------
    async def _run(self) -> None:
        c = self.cfg
        iv = INTERVAL_MS[c.interval]
        lead = int(c.lead_s * 1000)
        self._emit("status", f"Motor en marcha · {self.describe()}")
        self._journal("engine.start", {"config": asdict(c)})
        errors = 0
        try:
            while self.running:
                await self._maybe_sync()
                boundary = next_tick(self._clock(), iv, lead)
                self.next_tick_ms = boundary
                await self._sleep_until(boundary - lead)
                if not self.running:
                    break
                try:
                    signals = await self._analyze(boundary)
                    errors = 0
                except BinanceError as e:
                    errors += 1
                    self._emit("error", f"análisis falló ({errors}/{MAX_TICK_ERRORS}): {e}")
                    if errors >= MAX_TICK_ERRORS:
                        self._halt("demasiados fallos de conexión seguidos")
                    continue
                if not signals or not self.running:
                    continue
                fire_at = boundary + int(c.entry_delay_s * 1000)
                await self._sleep_until(fire_at)
                if not self.running:
                    break
                late = self._clock() - fire_at
                if late > c.max_late_s * 1000:
                    self._emit("skip", f"análisis tardío ({late / 1000:.1f}s): salto esta vela")
                    continue
                for s in signals:
                    self._launch(s)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - un bug nunca debe dejarlo operando a ciegas
            self._halt(f"error inesperado en el motor: {e!r}")
        finally:
            self.running = False
            self.next_tick_ms = None
            self._emit("stopped", f"Motor detenido · {self.paused_reason or 'fin'}")

    # --- Análisis (T − lead) ----------------------------------------------------
    async def _held_symbols(self) -> set:
        positions = await self.client.position_risk()
        return {p["symbol"] for p in positions if float(p.get("positionAmt", 0) or 0) != 0}

    def _busy_reason(self, symbol: str, held: set, now: int) -> Optional[str]:
        if symbol in self.open_trades:
            return "el motor ya tiene una orden viva"
        if symbol in held:
            return "hay una posición abierta que no es del motor"
        if now < self._cooldown_until.get(symbol, 0):
            return "enfriamiento tras la última orden"
        return None

    async def _analyze(self, boundary: int) -> list[Signal]:
        if self.cfg.mode == "radar":
            cands = await self._radar_signals(boundary)
        else:
            cands = await self._candle_signals(boundary)
        return await self._admit(cands)

    async def _candle_signals(self, boundary: int) -> list[Signal]:
        c = self.cfg
        iv = INTERVAL_MS[c.interval]
        held = await self._held_symbols()
        sem = asyncio.Semaphore(6)

        async def fetch(sym):
            async with sem:
                try:
                    return sym, await self.client.klines(sym, c.interval, limit=KLINES_LIMIT)
                except BinanceError as e:
                    self._emit("error", f"{sym}: no pude leer velas ({e})")
                    return sym, None

        results = await asyncio.gather(*(fetch(s) for s in c.symbols))
        if not any(kl for _, kl in results):
            raise BinanceError("no llegaron velas de ningún símbolo")
        now = self._clock()
        exit_at = boundary + c.hold_candles * iv - int((c.lead_s + c.exit_margin_s) * 1000)
        out: list[Signal] = []
        for sym, kl in results:
            if not kl:
                continue
            self.shadow.resolve_klines(sym, kl, now)
            d = decide(kl, c.signal)
            self.shadow.record(sym, boundary, d.votes, d.side)
            self._journal("decision", {
                "symbol": sym, "candle_open": boundary, "side": d.side, "score": d.score,
                "votes": d.votes, "values": d.values, "price": d.price,
                "tp_pct": d.tp_pct, "sl_pct": d.sl_pct, "reason": d.reason})
            if d.side is None:
                self._emit("skip", f"{sym}: {d.reason}", symbol=sym, votes=d.votes)
                continue
            busy = self._busy_reason(sym, held, now)
            if busy:
                self._emit("skip", f"{sym}: {_side_label(d.side)} descartado ({busy})",
                           symbol=sym, votes=d.votes)
                continue
            out.append(Signal(sym, d.side, d.tp_pct, d.sl_pct, exit_at, float(d.score),
                              d.reason, "vela", votes=d.votes, price=d.price))
        return out

    async def _radar_signals(self, boundary: int) -> list[Signal]:
        c = self.cfg
        held = await self._held_symbols()
        if c.symbols:
            symbols = list(c.symbols)
        else:
            tickers = await market_mod.fetch_tickers(self.scan_client,
                                                     allowed=self.filters.symbols())
            symbols = [t.symbol for t in sorted(tickers, key=lambda t: t.quote_volume,
                                                reverse=True)[:c.radar_universe]]
        setups = await scanner.scan_market(
            self.scan_client, symbols, top_n=3,
            capacity=float(c.margin_usdt) * c.leverage, min_notional=5.0)
        if not setups:
            self._emit("skip", f"radar: sin setups accionables en {len(symbols)} símbolos")
            return []
        now = self._clock()
        exit_at = boundary + c.radar_hold_min * 60_000
        out: list[Signal] = []
        for s in setups:
            side = "BUY" if s.side == "long" else "SELL"
            entry = s.entry_high if s.side == "long" else s.entry_low
            if not entry:
                continue
            sl_pct = abs(entry - s.stop) / entry
            tp_pct = abs(s.target - entry) / entry
            self._journal("decision", {
                "symbol": s.symbol, "source": "radar", "side": side, "score": s.score,
                "components": s.components, "entry": entry, "stop": s.stop,
                "target": s.target, "rr_net": s.rr_net, "reason": s.reason})
            if s.score < c.radar_min_score:
                self._emit("skip", f"{s.symbol}: score {s.score:g} < {c.radar_min_score:g}",
                           symbol=s.symbol)
                continue
            busy = self._busy_reason(s.symbol, held, now)
            if busy:
                self._emit("skip", f"{s.symbol}: {_side_label(side)} descartado ({busy})",
                           symbol=s.symbol)
                continue
            out.append(Signal(s.symbol, side, tp_pct, sl_pct, exit_at, float(s.score),
                              f"{s.reason} · R:R {s.rr_net}", "radar",
                              votes=dict(s.components), price=entry))
        return out

    async def _admit(self, cands: list[Signal]) -> list[Signal]:
        """Dimensiona, pasa el riesgo y deja el símbolo preparado. Solo sale lo que
        se va a disparar al abrir la vela."""
        if not cands:
            return []
        c = self.cfg
        if self.stats.opened >= c.max_trades:
            self._halt(self._cap_reason(bool(self.open_trades)))
            return []
        slots = min(c.max_trades - self.stats.opened, c.max_concurrent - len(self.open_trades))
        cap = min(int(c.leverage), int(self.risk.limits.max_leverage))
        cands.sort(key=lambda s: abs(s.score), reverse=True)
        out: list[Signal] = []
        for s in cands:
            if len(out) >= slots:
                self._emit("skip", f"{s.symbol}: sin hueco (máx {c.max_concurrent} órdenes "
                                   f"del motor a la vez)", symbol=s.symbol)
                continue
            try:
                sf = self.filters.get(s.symbol)
            except BinanceError as e:
                self._emit("skip", f"{s.symbol}: {e}", symbol=s.symbol)
                continue
            lev = safe_leverage(s.sl_pct, cap)
            notional = Decimal(str(c.margin_usdt)) * lev
            if notional < sf.min_notional:
                self._emit("skip", f"{s.symbol}: notional {notional} USDT < mínimo "
                                   f"{sf.min_notional} del símbolo (sube el margen)",
                           symbol=s.symbol)
                continue
            try:
                await self.risk.check_open(s.symbol, notional)
            except KillSwitchError as e:
                self._halt(str(e))
                return []
            except RiskError as e:
                self._emit("skip", f"{s.symbol}: bloqueada por riesgo: {e}", symbol=s.symbol)
                continue
            try:
                await self._prepare_symbol(s.symbol, lev)
            except BinanceError as e:
                self._emit("skip", f"{s.symbol}: no pude fijar apalancamiento ({e})",
                           symbol=s.symbol)
                continue
            s.leverage, s.notional = lev, notional
            out.append(s)
        return out

    def _cap_reason(self, others_live: bool) -> str:
        live = " (las abiertas siguen supervisadas)" if others_live else ""
        return f"tope de {self.cfg.max_trades} órdenes de la sesión alcanzado{live}"

    async def _prepare_symbol(self, symbol: str, leverage: int) -> None:
        """Margen aislado + apalancamiento ANTES de la vela (no en el instante de entrar)."""
        if self._configured.get(symbol) == leverage:
            return
        try:
            await self.trader.set_margin_type(symbol, "ISOLATED")
        except BinanceAPIError:
            pass  # no cambiable ahora (p.ej. órdenes abiertas); no bloquea, igual que open_scalp
        await self.trader.set_leverage(symbol, leverage)
        self._configured[symbol] = leverage

    # --- Ejecución (T + delay) ----------------------------------------------------
    def _launch(self, s: Signal) -> None:
        rec = TradeRecord(
            symbol=s.symbol, side=s.side, source=s.source, score=s.score, reason=s.reason,
            votes=s.votes, leverage=s.leverage, notional=s.notional, tp_pct=s.tp_pct,
            sl_pct=s.sl_pct, exit_at_ms=s.exit_at_ms, opened_ms=self._clock())
        self.open_trades[s.symbol] = rec   # reservado ANTES de cualquier await
        self.stats.opened += 1
        task = asyncio.create_task(self._execute(rec))
        self._trade_tasks.add(task)
        task.add_done_callback(self._trade_tasks.discard)

    async def _execute(self, rec: TradeRecord) -> None:
        c = self.cfg
        sym = rec.symbol
        try:
            scfg = ScalpConfig(
                notional=rec.notional, take_profit_pct=rec.tp_pct, stop_loss_pct=rec.sl_pct,
                max_hold_seconds=max(1, int((rec.exit_at_ms - self._clock()) / 1000)),
                leverage=rec.leverage, entry_type=c.entry_type,
                entry_timeout_s=c.maker_timeout_s, margin_type="ISOLATED",
                preconfigured=True)
            try:
                res = await open_scalp(self.trader, self.client, symbol=sym, side=rec.side,
                                       config=scfg)
            except Exception as e:  # noqa: BLE001
                self.stats.opened -= 1
                self.stats.failed += 1
                self._entry_failures += 1
                self._emit("error", f"{sym}: no se pudo abrir ({e})", symbol=sym)
                self._journal("trade.error", {"symbol": sym, "side": rec.side, "error": str(e)})
                await self._safety_close(sym, label="posición residual cerrada tras el fallo")
                if self._entry_failures >= MAX_ENTRY_FAILURES:
                    self._halt(f"{self._entry_failures} entradas fallidas seguidas "
                               f"(revisa saldo/permisos de la cuenta)")
                return
            self._entry_failures = 0
            if not res.get("filled", True):
                self.stats.opened -= 1
                self._emit("skip", f"{sym}: la entrada maker no se llenó; cancelada", symbol=sym)
                return
            rec.qty = str(res.get("qty"))
            rec.entry = float(res["mark"])
            rec.tp_trigger = float(res["tp_trigger"]) if res.get("tp_trigger") else None
            rec.sl_trigger = float(res["sl_trigger"]) if res.get("sl_trigger") else None
            rec.bracket_ok = not res.get("errors")
            self._emit("open", (f"{sym} {_side_label(rec.side)} · {rec.notional} USDT "
                                f"({rec.leverage}x) a ~{rec.entry:g} · TP {rec.tp_pct * 100:.2f}% "
                                f"/ SL {rec.sl_pct * 100:.2f}% · sale {_hhmmss(rec.exit_at_ms)} · "
                                f"{rec.reason}"), symbol=sym)
            if res.get("errors"):
                self._emit("error", f"{sym}: bracket incompleto {res['errors']}; la salida "
                                    f"por tiempo sigue activa", symbol=sym)
            self._journal("trade.open", {
                "symbol": sym, "side": rec.side, "source": rec.source, "qty": rec.qty,
                "entry": rec.entry, "notional": rec.notional, "leverage": rec.leverage,
                "tp_trigger": rec.tp_trigger, "sl_trigger": rec.sl_trigger,
                "exit_at_ms": rec.exit_at_ms, "votes": rec.votes, "score": rec.score,
                "reason": rec.reason, "bracket_errors": res.get("errors")})
            await self._supervise(rec, res)
            await self._settle(rec)
        finally:
            self.open_trades.pop(sym, None)

    async def _supervise(self, rec: TradeRecord, res: dict) -> None:
        c = self.cfg
        hold_s = max(1.0, (rec.exit_at_ms - self._clock()) / 1000.0)
        sl_id = (res.get("sl") or {}).get("algoId")
        # Sin trailing: disparador infinito -> el stop nunca se mueve, solo se
        # vigila el cierre (TP/SL) y la salida por tiempo.
        be_trigger = max(rec.tp_pct * 0.5, 0.001) if c.trailing else math.inf
        try:
            await manage_trailing_stop(
                self.trader, self.client, symbol=rec.symbol, side=rec.side, entry=rec.entry,
                initial_stop=res.get("sl_trigger"), sl_algo_id=sl_id,
                be_trigger_pct=be_trigger, trail_pct=c.trail_pct,
                max_hold_seconds=hold_s, poll_seconds=c.poll_s)
        except Exception as e:  # noqa: BLE001
            self._emit("error", f"{rec.symbol}: fallo supervisando ({e}); cierro por seguridad",
                       symbol=rec.symbol)
        rec.exit_reason = "tiempo" if self._clock() >= rec.exit_at_ms - 1000 else "bracket"
        # Idempotente: cierra lo que quede y cancela el bracket huérfano.
        await self._safety_close(rec.symbol, label="posición residual cerrada a mercado")

    async def _safety_close(self, symbol: str, *, label: str) -> None:
        try:
            if await close_scalp(self.trader, self.client, symbol):
                self._emit("close", f"{symbol}: {label}", symbol=symbol)
        except Exception as e:  # noqa: BLE001
            self._emit("error", f"{symbol}: no pude verificar/cerrar ({e}). "
                                f"REVISA la posición en Binance.", symbol=symbol)

    async def _settle(self, rec: TradeRecord) -> None:
        """PnL NETO real desde userTrades (realizado − comisiones) y estadísticas."""
        c = self.cfg
        await self._sleep(1.0)  # que los fills del cierre aparezcan en userTrades
        try:
            fills = await self.client.user_trades(
                rec.symbol, startTime=rec.opened_ms - 2000, limit=100)
            realized = sum(float(f.get("realizedPnl", 0) or 0) for f in fills)
            comm = sum(float(f.get("commission", 0) or 0) for f in fills
                       if (f.get("commissionAsset") or "USDT") == "USDT")
            rec.net, rec.commission = realized - comm, comm
        except BinanceError as e:
            self._emit("error", f"{rec.symbol}: no pude leer el PnL ({e})", symbol=rec.symbol)
        rec.closed_ms = self._clock()
        if rec.exit_reason == "bracket" and rec.net is not None:
            rec.exit_reason = "TP/trailing" if rec.net > 0 else "SL"
        self.stats.record(rec.net, rec.commission)
        self.closed_trades.append(rec)
        if c.cooldown_candles > 0:
            self._cooldown_until[rec.symbol] = (rec.closed_ms
                                                + c.cooldown_candles * INTERVAL_MS[c.interval])
        st = self.stats
        net_txt = "PnL desconocido" if rec.net is None else f"neto {rec.net:+.4f} USDT"
        wr = "" if st.win_rate is None else f" ({st.win_rate * 100:.0f}%)"
        self._emit("close", (f"{rec.symbol} {_side_label(rec.side)} cerrada ({rec.exit_reason}) · "
                             f"{net_txt} · sesión {st.wins}/{st.wins + st.losses}{wr} · "
                             f"total {st.net:+.4f} USDT"),
                   symbol=rec.symbol, net=rec.net)
        self._journal("trade.close", {
            "symbol": rec.symbol, "side": rec.side, "source": rec.source,
            "exit_reason": rec.exit_reason, "net": rec.net, "commission": rec.commission,
            "opened_ms": rec.opened_ms, "closed_ms": rec.closed_ms, "votes": rec.votes,
            "score": rec.score, "stats": asdict(st)})
        if rec.net is not None and st.consec_losses >= c.max_consecutive_losses:
            self._halt(f"racha de {st.consec_losses} pérdidas seguidas: pausa de seguridad "
                       f"(sin martingala; revisa antes de reanudar)")
        elif st.opened >= c.max_trades:
            # Esta orden aún figura en open_trades (se retira al terminar _execute).
            self._halt(self._cap_reason(len(self.open_trades) > 1))
