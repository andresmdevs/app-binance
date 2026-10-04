"""Motor autónomo: orquestación con cliente y reloj FALSOS (sin red, sin esperar).

El reloj falso avanza cuando el motor "duerme", así una sesión de varios minutos
de mercado corre en milisegundos. `decide` y `manage_trailing_stop` se sustituyen
por dobles: aquí se prueba el CICLO (cuándo analiza, cuándo dispara, riesgo,
paradas), no los indicadores (eso va en test_autosignal).
"""
import asyncio
from decimal import Decimal

import pytest

from binance import engine as eng_mod
from binance.autosignal import Decision
from binance.client import BinanceAPIError
from binance.engine import (AutoEngine, EngineConfig, EngineError, next_tick,
                            radar_config, safe_leverage)
from binance.filters import parse_symbol_filters
from binance.risk import KillSwitchError, RiskError, RiskLimits
from binance.signals import Setup
from binance.trade import FuturesTrader
from conftest import FAKE_BTC, FakeClient

T0 = 1_700_000_010_000          # 10 s dentro de un minuto
MIN = 60_000


def run(coro):
    return asyncio.run(coro)


def drive(e):
    """Arranca y espera al motor con tope: un fallo debe fallar, no colgarse."""
    async def go():
        e.start()
        await asyncio.wait_for(e.join(), timeout=5)
    run(go())


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        # Primero cede el turno (las demás tareas corren "en este instante") y
        # luego avanza el reloj: así se comporta el tiempo real.
        await asyncio.sleep(0)
        self.t += int(s * 1000)


class EngineFake(FakeClient):
    """Exchange simulado: las órdenes MARKET abren/cierran posición de verdad."""
    testnet = True
    time_offset = 0

    def __init__(self, clock, *, fills=None, kline_delay_ms=0, **kw):
        kw.setdefault("responses", {"/fapi/v1/openAlgoOrders": []})
        super().__init__(mark="100", **kw)
        self.clock = clock
        self.fills = fills if fills is not None else [
            {"realizedPnl": "0", "commission": "0.0125", "commissionAsset": "USDT"},
            {"realizedPnl": "0.5", "commission": "0.0125", "commissionAsset": "USDT"}]
        self.kline_delay_ms = kline_delay_ms
        self.pos = {}
        self.times = []          # (método, path, hora del reloj)

    async def sync_time(self):
        return 0

    async def klines(self, symbol, interval, limit=500, **kw):
        self.clock.t += self.kline_delay_ms
        t = self.clock.t
        return [[t - 60_000, 100, 101, 99, 100, 10, t - 1, 1000, 1, 5, 500, 0]]

    async def position_risk(self, symbol=None):
        return [{"symbol": s, "positionAmt": str(q), "entryPrice": "100"}
                for s, q in self.pos.items() if q and (symbol is None or s == symbol)]

    async def user_trades(self, symbol, limit=500, **kw):
        return self.fills

    async def _request(self, method, path, params=None, *, signed=False):
        self.times.append((method, path, self.clock.t))
        r = await super()._request(method, path, params, signed=signed)
        if method == "POST" and path == "/fapi/v1/order":
            q = float(params["quantity"])
            s = params["symbol"]
            if params.get("reduceOnly") == "true":
                self.pos[s] = 0
            else:
                self.pos[s] = q if params["side"] == "BUY" else -q
        return r


class FakeRisk:
    def __init__(self, exc=None):
        self.limits = RiskLimits()
        self.exc = exc
        self.calls = []

    async def check_open(self, symbol, notional):
        self.calls.append((symbol, notional))
        if self.exc:
            raise self.exc


class Journal:
    def __init__(self):
        self.rows = []

    def record(self, action, request=None, *a):
        self.rows.append((action, request))

    def actions(self):
        return [a for a, _ in self.rows]


BUY = Decision("BUY", 3, {"trend": 1, "macd": 1, "rsi": 0, "bollinger": 0, "flow": 1},
               {}, 100.0, 0.004, 0.006, 0.004, "suma +3: test")


@pytest.fixture
def patched(monkeypatch):
    """decide fijo + supervisión que simula que el bracket cerró la posición."""
    state = {"decision": BUY, "supervised": 0}
    monkeypatch.setattr(eng_mod, "decide", lambda kl, cfg=None: state["decision"])

    async def fake_manage(trader, client, *, symbol, **kw):
        state["supervised"] += 1
        client.pos[symbol] = 0          # TP/SL ejecutado en el exchange
    monkeypatch.setattr(eng_mod, "manage_trailing_stop", fake_manage)
    return state


def make(filters, clock=None, cfg=None, risk=None, client=None, **kw):
    clock = clock or Clock()
    client = client or EngineFake(clock)
    journal = Journal()
    e = AutoEngine(client, FuturesTrader(client, filters), risk or FakeRisk(),
                   cfg or EngineConfig(symbols=["BTCUSDT"], max_trades=1),
                   journal=journal, clock=clock, sleep=clock.sleep, **kw)
    return e, client, clock, journal


def stop_on(engine_ref, needle):
    def cb(evt):
        if needle in evt["msg"] and engine_ref:
            engine_ref[0].running = False
    return cb


# --- Funciones puras ---------------------------------------------------------------
def test_next_tick_aligns_to_candle_open():
    b = next_tick(T0, MIN, 4000)
    assert b % MIN == 0 and b - 4000 >= T0 and b - MIN < T0
    # dentro de la ventana de anticipación (a 2 s de abrir) -> la vela siguiente
    late = b - 2000
    assert next_tick(late, MIN, 4000) == b + MIN
    # justo en el instante de análisis -> esa misma vela
    assert next_tick(b - 4000, MIN, 4000) == b


def test_safe_leverage_keeps_liquidation_beyond_stop():
    assert safe_leverage(0.002, 10) == 10           # SL corto -> manda el tope
    assert safe_leverage(0.05, 10) == 10
    assert safe_leverage(0.05, 20) == 12            # 1/(0.05·1.6) = 12.5
    assert safe_leverage(0.5, 10) == 1
    assert safe_leverage(0, 7) == 7


# --- Ciclo completo ---------------------------------------------------------------
def test_full_cycle_analyzes_before_and_fires_at_candle_open(filters, patched):
    e, c, clock, journal = make(filters)

    drive(e)

    boundary = next_tick(T0, MIN, 4000)
    orders = [(m, p, t) for m, p, t in c.times if p == "/fapi/v1/order" and m == "POST"]
    entry = orders[0]
    assert entry[2] >= boundary                          # dispara con la vela ya abierta
    assert entry[2] - boundary < 2000
    lev = [t for m, p, t in c.times if p == "/fapi/v1/leverage"]
    assert lev and lev[0] < boundary                     # preparado ANTES de la vela
    params = c.find("POST", "/fapi/v1/order")[0]["params"]
    assert params["side"] == "BUY" and params["type"] == "MARKET"
    algos = c.find("POST", "/fapi/v1/algoOrder")
    assert sorted(a["params"]["type"] for a in algos) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert patched["supervised"] == 1
    st = e.stats
    assert (st.opened, st.closed, st.wins, st.losses) == (1, 1, 1, 0)
    assert st.net == pytest.approx(0.475)                # 0.5 realizado − 0.025 comisiones
    assert "tope" in e.paused_reason
    assert {"engine.start", "decision", "trade.open", "trade.close"} <= set(journal.actions())
    assert not e.open_trades


def test_refuses_production(filters):
    clock = Clock()
    client = EngineFake(clock)
    client.testnet = False
    e, *_ = make(filters, clock=clock, client=client)
    with pytest.raises(EngineError, match="TESTNET"):
        e.start()


def test_rejects_unknown_symbol(filters):
    e, *_ = make(filters, cfg=EngineConfig(symbols=["NOPEUSDT"]))
    with pytest.raises(EngineError, match="NOPEUSDT"):
        e.start()


def test_kill_switch_stops_engine_without_orders(filters, patched):
    risk = FakeRisk(KillSwitchError("KILL-SWITCH: pérdida diaria"))
    e, c, *_ = make(filters, risk=risk, cfg=EngineConfig(symbols=["BTCUSDT"]))

    drive(e)
    assert not c.find("POST", "/fapi/v1/order")
    assert "KILL-SWITCH" in e.paused_reason


def test_other_risk_block_only_skips(filters, patched):
    ref = []
    risk = FakeRisk(RiskError("Máx. de posiciones"))
    e, c, *_ = make(filters, risk=risk, on_event=stop_on(ref, "bloqueada por riesgo"))
    ref.append(e)

    drive(e)
    assert not c.find("POST", "/fapi/v1/order")
    assert risk.calls                       # se consultó el riesgo...
    assert e.paused_reason is None          # ...y solo se saltó la señal (no se detuvo)


def test_skips_symbol_with_foreign_position(filters, patched):
    ref = []
    e, c, *_ = make(filters, on_event=stop_on(ref, "no es del motor"))
    ref.append(e)
    c.pos["BTCUSDT"] = 0.5                  # posición manual del usuario

    drive(e)
    assert not c.find("POST", "/fapi/v1/order")
    assert c.pos["BTCUSDT"] == 0.5          # jamás la toca


def test_weak_signal_records_shadow_and_skips(filters, patched):
    patched["decision"] = Decision(None, 1, {"trend": 1, "macd": 0}, {}, 100.0, 0.004,
                                   0.006, 0.004, "señal débil: suma +1")
    ref = []
    e, c, *_ = make(filters, on_event=stop_on(ref, "señal débil"))
    ref.append(e)

    drive(e)
    assert not c.find("POST", "/fapi/v1/order")
    assert e.shadow._pending                # la vela quedó anotada en el marcador


def test_late_analysis_skips_candle(filters, patched):
    ref = []
    clock = Clock()
    client = EngineFake(clock, kline_delay_ms=7000)   # análisis de 7 s > 4 s + 2 s
    e, c, *_ = make(filters, clock=clock, client=client,
                    on_event=stop_on(ref, "tardío"))
    ref.append(e)

    drive(e)
    assert not c.find("POST", "/fapi/v1/order")


def test_losing_streak_pauses(filters, patched):
    clock = Clock()
    client = EngineFake(clock, fills=[
        {"realizedPnl": "-0.3", "commission": "0.02", "commissionAsset": "USDT"}])
    cfg = EngineConfig(symbols=["BTCUSDT"], max_trades=10, max_consecutive_losses=2)
    e, c, *_ = make(filters, clock=clock, client=client, cfg=cfg)

    drive(e)
    assert e.stats.losses == 2 and e.stats.wins == 0
    assert "racha" in e.paused_reason
    assert e.stats.net == pytest.approx(-0.64)


def test_entry_failures_halt(filters, patched):
    clock = Clock()
    client = EngineFake(clock, raise_on={
        ("POST", "/fapi/v1/order"): BinanceAPIError(400, -2019, "Margin is insufficient.")})
    e, c, *_ = make(filters, clock=clock, client=client,
                    cfg=EngineConfig(symbols=["BTCUSDT"], max_trades=10))

    drive(e)
    assert e.stats.failed == 3 and e.stats.opened == 0
    assert "fallidas" in e.paused_reason


def test_concurrency_limit(filters, patched):
    eth = dict(FAKE_BTC, symbol="ETHUSDT")
    filters._symbols["ETHUSDT"] = parse_symbol_filters(eth)
    cfg = EngineConfig(symbols=["BTCUSDT", "ETHUSDT"], max_trades=1, max_concurrent=1)
    e, c, *_ = make(filters, cfg=cfg)

    drive(e)
    assert len(c.find("POST", "/fapi/v1/order")) == 1
    assert any("sin hueco" in ev["msg"] for ev in e.events)


def test_min_notional_skip(filters, patched):
    ref = []
    cfg = EngineConfig(symbols=["BTCUSDT"], margin_usdt=Decimal("0.5"), leverage=2)
    e, c, *_ = make(filters, cfg=cfg, on_event=stop_on(ref, "sube el margen"))
    ref.append(e)

    drive(e)
    assert not c.find("POST", "/fapi/v1/order")


def test_stop_closes_engine_positions(filters, monkeypatch):
    monkeypatch.setattr(eng_mod, "decide", lambda kl, cfg=None: BUY)
    hold = asyncio.Event()

    async def slow_manage(trader, client, *, symbol, **kw):
        hold.set()
        while client.pos.get(symbol):     # sigue abierta hasta que alguien la cierre
            await asyncio.sleep(0)
    monkeypatch.setattr(eng_mod, "manage_trailing_stop", slow_manage)
    e, c, *_ = make(filters, cfg=EngineConfig(symbols=["BTCUSDT"], max_trades=5))

    async def go():
        e.start()
        await hold.wait()
        await e.stop(close_positions=True)
        await e.join()
    run(go())
    closes = [x for x in c.find("POST", "/fapi/v1/order")
              if x["params"].get("reduceOnly") == "true"]
    assert closes and c.pos["BTCUSDT"] == 0
    assert e.stats.closed == 1 and not e.running


def test_radar_mode_uses_setup_levels(filters, patched, monkeypatch):
    setup = Setup(symbol="BTCUSDT", side="long", entry_low=99.0, entry_high=100.0,
                  stop=98.0, target=103.0, rr_net=1.9, score=72.0,
                  components={"structure": 0.8}, reason="rebote en soporte")

    async def fake_scan(cli, symbols, **kw):
        assert symbols == ["BTCUSDT"]
        return [setup]
    monkeypatch.setattr(eng_mod.scanner, "scan_market", fake_scan)
    cfg = radar_config(symbols=["BTCUSDT"], max_trades=1)
    e, c, *_ = make(filters, cfg=cfg, scan_client=object())

    drive(e)
    algos = {a["params"]["type"]: a["params"] for a in c.find("POST", "/fapi/v1/algoOrder")}
    assert algos["TAKE_PROFIT_MARKET"]["triggerPrice"] == "103.0"   # +3% sobre la marca
    assert algos["STOP_MARKET"]["triggerPrice"] == "98.0"           # −2%
    assert e.stats.opened == 1


def test_radar_requires_scan_client(filters):
    e, *_ = make(filters, cfg=radar_config(symbols=["BTCUSDT"]))
    with pytest.raises(EngineError, match="radar"):
        e.start()


def test_failed_last_entry_does_not_count_against_cap(filters, patched):
    """Si la única orden permitida falla al abrir, el tope no se consume."""
    clock = Clock()
    client = EngineFake(clock, raise_on={
        ("POST", "/fapi/v1/order"): BinanceAPIError(400, -2019, "Margin is insufficient.")})
    e, c, *_ = make(filters, clock=clock, client=client)     # max_trades=1
    drive(e)
    assert e.stats.opened == 0 and e.stats.failed == 3
    assert "fallidas" in e.paused_reason and "tope" not in e.paused_reason
