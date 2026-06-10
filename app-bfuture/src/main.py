"""App de Binance USDⓈ-M Futures (Flet).

UI reactiva sobre la capa `binance/`:
  * Dashboard  -> cuenta + posiciones (positionRisk v3) + alta de órdenes reales.
  * Órdenes    -> órdenes abiertas reales + cancelar.
  * Gráfico    -> klines por REST + línea de precio EN VIVO por WebSocket.
  * Historial  -> PnL realizado con filtros (símbolo, días, solo positivo, min PnL).

Monitoreo EN VIVO por WebSocket (hito 4): con "Auto-actualizar" se abre el user
data stream (refresca por eventos, sin polling agresivo) y un stream de markPrice
para mover el PnL de las posiciones en tiempo real. Un poll lento de respaldo cubre
caídas del stream.

Por defecto opera contra TESTNET (BINANCE_TESTNET=true). Abrir/cerrar posición pide
confirmación y toda orden se valida con los filtros del símbolo.
"""
import asyncio
import os
import secrets as _secrets
from decimal import Decimal
from pathlib import Path

import flet as ft
from dotenv import load_dotenv

from binance import (
    AuditLog,
    BinanceError,
    BinanceFuturesClient,
    FilterCache,
    FuturesTrader,
    MarketStream,
    RiskError,
    RiskLimits,
    RiskManager,
    ScalpConfig,
    UserStream,
    close_scalp,
    manage_trailing_stop,
    open_scalp,
)
from binance import autoscalp
from binance import market as market_mod
from binance import pnl as pnl_mod
from binance.autoscalp import auto_levels, momentum_ok
from binance.forwardtest import select_candidates

# --- Configuración / credenciales --------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # .../app-bfuture
load_dotenv(PROJECT_ROOT / ".env")

API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_API_SECRET")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y")


TESTNET = _env_bool("BINANCE_TESTNET", True)
FALLBACK_POLL_SECONDS = 30  # respaldo por si el WS cae
CHART_POINTS = 200


def _env_dec(name: str, default) -> Decimal:
    v = os.getenv(name)
    try:
        return Decimal(v) if v else Decimal(str(default))
    except Exception:
        return Decimal(str(default))


# Guardas de riesgo (configurables por entorno).
RISK_LIMITS = RiskLimits(
    max_order_notional=_env_dec("RISK_MAX_ORDER_NOTIONAL", 1000),
    max_position_notional=_env_dec("RISK_MAX_POSITION_NOTIONAL", 5000),
    max_open_positions=int(os.getenv("RISK_MAX_OPEN_POSITIONS", "5")),
    max_leverage=int(os.getenv("RISK_MAX_LEVERAGE", "10")),
    daily_loss_limit=_env_dec("RISK_DAILY_LOSS_LIMIT", 100),
)
DEFAULT_STOP_PCT = float(os.getenv("ENTRY_STOP_PCT", "2"))   # % de stop por defecto
DEFAULT_TP_PCT = float(os.getenv("ENTRY_TP_PCT", "1"))       # % take-profit por defecto
DEFAULT_MAX_HOLD_MIN = float(os.getenv("SCALP_MAX_HOLD_MIN", "5"))  # cierre por tiempo
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))

# Modo Auto (el bot calcula símbolo/TP/SL/cantidad).
AUTO_TARGET_ROE = float(os.getenv("AUTO_TARGET_ROE", "0.35"))  # 35% ROE objetivo
AUTO_STOP_ROE = float(os.getenv("AUTO_STOP_ROE", "0.5"))      # pérdida máx % del margen
AUTO_MARGIN = float(os.getenv("AUTO_MARGIN", "0.30"))         # capital/orden en Auto
AUTO_MAX_PRICE = float(os.getenv("AUTO_MAX_PRICE", "0.5"))    # solo monedas baratas
AUTO_MIN_VOL = float(os.getenv("AUTO_MIN_VOL", "1000000"))    # liquidez mínima
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "0.005"))           # trailing del stop (0.5%)

GREEN = ft.Colors.GREEN_ACCENT_400
RED = ft.Colors.RED_ACCENT_200
GREY = ft.Colors.GREY_400


def fmt_usd(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def pnl_color(x) -> str:
    try:
        return GREEN if float(x) >= 0 else RED
    except (TypeError, ValueError):
        return GREY


def fmt_price(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    if v >= 1000:
        return f"{v:,.1f}"
    if v >= 1:
        return f"{v:,.3f}"
    return f"{v:.6f}"


class TradingApp(ft.Column):
    def __init__(self, page: ft.Page, client: BinanceFuturesClient, audit=None):
        super().__init__(expand=True, spacing=0)
        self.page = page
        self.client = client
        self.filters = FilterCache(client)
        self.trader = FuturesTrader(client, self.filters, audit=audit)
        self.risk = RiskManager(client, RISK_LIMITS)
        self.is_running = False

        # Estado de los controles de orden (autocompletar / sliders).
        self._syncing = False
        self._leverage = min(DEFAULT_LEVERAGE, RISK_LIMITS.max_leverage)
        self._available_balance = 0.0
        self._all_symbols: list[str] = []
        self._mark_cache: dict[str, float] = {}

        # Estado de streams / monitoreo.
        self._user_stream = None
        self._mark_stream = None
        self._mark_symbols: list[str] = []
        self._fallback_task = None
        self._refresh_task = None
        self._pos_state: dict[str, dict] = {}  # symbol -> {entry, amt, pnl_text, mark_text, cur_pnl}

        # Estado del gráfico.
        self._chart_stream = None
        self._chart_closes: list[float] = []
        self._chart_last_open = None
        self._chart_symbol_active = None

        # --- Cabecera --------------------------------------------------------
        self.env_badge = ft.Container(
            content=ft.Text(
                "TESTNET" if TESTNET else "● REAL ●", size=11, weight=ft.FontWeight.BOLD,
                color=ft.Colors.BLACK if TESTNET else ft.Colors.WHITE,
            ),
            bgcolor=ft.Colors.AMBER_400 if TESTNET else ft.Colors.RED_700,
            padding=ft.padding.symmetric(horizontal=10, vertical=4), border_radius=20,
        )
        self.status_text = ft.Text("Conectando…", size=12, color=GREY)
        self.weight_text = ft.Text("", size=11, color=GREY)
        self.run_switch = ft.Switch(
            label="Auto-actualizar", value=False, on_change=self.toggle_monitoring,
            adaptive=True,
        )

        # --- Dashboard -------------------------------------------------------
        self.acct_balance = ft.Text("—", size=18, weight=ft.FontWeight.BOLD)
        self.acct_upnl = ft.Text("—", size=14)
        self.acct_avail = ft.Text("—", size=13, color=GREY)
        self.acct_margin_ratio = ft.Text("—", size=13, color=GREY)
        self.acct_daily = ft.Text("", size=13, color=GREY)
        self.positions_list = ft.Column(spacing=10, tight=True)

        # --- Símbolo con autocompletado dinámico ---
        self.symbol_field = ft.TextField(
            label="Símbolo (escribe y elige de la lista)", value="BTCUSDT", width=320,
            dense=True, text_size=14, capitalization=ft.TextCapitalization.CHARACTERS,
            hint_text="BTC, SOL, PEPE…", on_change=self._on_symbol_change,
        )
        self.symbol_suggestions = ft.Column(spacing=2, tight=True, visible=False)
        self.symbol_hint = ft.Text("", size=11, color=GREY)

        # --- Apalancamiento (slider) ---
        self.leverage_slider = ft.Slider(
            min=1, max=RISK_LIMITS.max_leverage,
            divisions=max(1, RISK_LIMITS.max_leverage - 1),
            value=self._leverage, label="{value}x", on_change=self._on_leverage, expand=True)
        self.leverage_label = ft.Text(f"{self._leverage}x", weight=ft.FontWeight.BOLD, width=46)

        # --- Tamaño (notional): slider + campo numérico sincronizados ---
        self.notional_field = ft.TextField(
            label="Tamaño", value="100", width=160, dense=True, text_size=14,
            suffix_text="USDT", keyboard_type=ft.KeyboardType.NUMBER,
            on_change=self._on_notional_field, helper_text="margen × apalancamiento")
        self.notional_slider = ft.Slider(
            min=5, max=1000, value=100, label="{value}",
            on_change=self._on_notional_slider, expand=True)

        # --- TP / SL / tiempo / maker (con unidades y ayuda) ---
        self.tp_field = ft.TextField(
            value=str(DEFAULT_TP_PCT), width=140, dense=True, text_size=14,
            suffix_text="%", keyboard_type=ft.KeyboardType.NUMBER,
            on_change=self._on_levels_change)
        self.stop_field = ft.TextField(
            value=str(DEFAULT_STOP_PCT), width=140, dense=True, text_size=14,
            suffix_text="%", keyboard_type=ft.KeyboardType.NUMBER,
            on_change=self._on_levels_change)
        self.maxhold_field = ft.TextField(
            value=str(DEFAULT_MAX_HOLD_MIN), width=130, dense=True, text_size=14,
            suffix_text="min", keyboard_type=ft.KeyboardType.NUMBER)
        self.maker_switch = ft.Switch(label="Maker (entrada límite, sin fee taker)",
                                      value=True, adaptive=True)
        self.margin_field = ft.TextField(
            label="Margen (modo Auto)", value=str(AUTO_MARGIN), width=170, dense=True,
            text_size=14, suffix_text="USDT", keyboard_type=ft.KeyboardType.NUMBER,
            helper_text="capital por orden · notional = margen × apalancamiento")
        self.auto_switch = ft.Switch(
            label="Auto: el bot elige símbolo y calcula TP/SL/cantidad (35% ROE + trailing)",
            value=False, adaptive=True, on_change=self._on_auto_toggle)
        self.liq_preview = ft.Text("", size=12, color=GREY)
        # Mensaje PERSISTENTE del resultado de la última orden (no se desvanece).
        self.order_status_text = ft.Text("", size=13, weight=ft.FontWeight.W_500)

        order_entry = ft.Container(
            content=ft.Column([
                ft.Text("Abrir scalp", weight=ft.FontWeight.BOLD, size=15),
                ft.Text("El bot añade automáticamente TP + Stop-loss + cierre por tiempo "
                        "a cada entrada.", size=11, color=GREY),
                ft.Text("1) Símbolo", size=12, color=GREY, weight=ft.FontWeight.BOLD),
                self.symbol_field,
                self.symbol_suggestions,
                self.symbol_hint,
                ft.Row([ft.Text("2) Apalancamiento", size=12, color=GREY,
                                weight=ft.FontWeight.BOLD, width=130),
                        self.leverage_slider, self.leverage_label],
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text("3) Tamaño de la posición (USDT) = margen × apalancamiento",
                        size=12, color=GREY, weight=ft.FontWeight.BOLD),
                ft.Row([self.notional_field, self.notional_slider,
                        ft.OutlinedButton("Mín", on_click=self._notional_min),
                        ft.OutlinedButton("Máx", on_click=self._notional_max)],
                       vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                ft.Text("4) Salidas", size=12, color=GREY, weight=ft.FontWeight.BOLD),
                ft.Row([
                    ft.Column([ft.Text("Take-profit (%)", size=11, color=GREY),
                               self.tp_field], tight=True, spacing=3),
                    ft.Column([ft.Text("Stop-loss (%)", size=11, color=GREY),
                               self.stop_field], tight=True, spacing=3),
                    ft.Column([ft.Text("Cierre máx (min)", size=11, color=GREY),
                               self.maxhold_field], tight=True, spacing=3),
                ], spacing=12, wrap=True),
                self.liq_preview,
                ft.Row([self.maker_switch]),
                ft.Row([self.auto_switch]),
                ft.Column([ft.Text("Margen por orden (USDT) — solo modo Auto", size=11,
                                   color=GREY), self.margin_field], tight=True, spacing=3),
                ft.Row([
                    ft.FilledButton("LONG", icon=ft.Icons.TRENDING_UP,
                                    on_click=self.on_open_long,
                                    style=ft.ButtonStyle(bgcolor=ft.Colors.GREEN_700)),
                    ft.FilledButton("SHORT", icon=ft.Icons.TRENDING_DOWN,
                                    on_click=self.on_open_short,
                                    style=ft.ButtonStyle(bgcolor=ft.Colors.RED_700)),
                ], spacing=12),
                self.order_status_text,
            ], spacing=10),
            padding=15, border_radius=12, bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.WHITE),
        )
        # Panel de mercado (resumen general + Top movers por ventana).
        self.market_summary = ft.Text("Mercado: —", size=12, color=GREY)
        self.market_tf = ft.Dropdown(
            label="Ventana", width=110, value="1d", dense=True,
            options=[ft.dropdown.Option(w) for w in ("1h", "4h", "1d", "5d", "1month")],
        )
        self.market_topn = ft.Dropdown(
            label="Top", width=90, value="10", dense=True,
            options=[ft.dropdown.Option(n) for n in ("5", "10", "25", "100")],
        )
        self.market_list = ft.ListView(spacing=2, height=220)
        market_panel = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text("Mercado", weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    self.market_tf, self.market_topn,
                    ft.IconButton(ft.Icons.REFRESH, tooltip="Actualizar mercado",
                                  on_click=self.on_load_market),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                self.market_summary,
                self.market_list,
            ], spacing=8),
            padding=15, border_radius=12, bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.WHITE),
        )

        dashboard = ft.Container(
            content=ft.Column([
                market_panel,
                ft.Container(height=6),
                ft.Container(
                    content=ft.Column([
                        ft.Text("Balance de margen", size=12, color=GREY),
                        self.acct_balance, self.acct_upnl,
                        ft.Row([self.acct_avail, self.acct_margin_ratio], spacing=20),
                        self.acct_daily,
                    ], spacing=4),
                    padding=15, border_radius=12,
                    bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.WHITE),
                ),
                ft.Container(height=6), order_entry, ft.Container(height=6),
                ft.Text("Posiciones abiertas", weight=ft.FontWeight.BOLD),
                self.positions_list,
            ], spacing=8, scroll=ft.ScrollMode.AUTO),
            padding=16,
        )

        # --- Órdenes ----------------------------------------------------------
        self.open_orders_list = ft.ListView(spacing=8, expand=True)
        orders_tab = ft.Container(content=self.open_orders_list, expand=True, padding=12)

        # --- Gráfico ----------------------------------------------------------
        self.chart_symbol = ft.TextField(
            label="Símbolo", value="BTCUSDT", width=140, dense=True, text_size=14,
            capitalization=ft.TextCapitalization.CHARACTERS,
        )
        self.chart_interval = ft.Dropdown(
            label="Intervalo", width=110, value="1h", dense=True,
            options=[ft.dropdown.Option(i) for i in ("1m", "5m", "15m", "1h", "4h", "1d")],
        )
        self.chart_last = ft.Text("—", size=16, weight=ft.FontWeight.BOLD)
        self.chart_change = ft.Text("", size=13, color=GREY)
        self.price_chart = ft.LineChart(
            data_series=[], expand=True, animate=0,
            border=ft.border.all(1, ft.Colors.with_opacity(0.1, ft.Colors.WHITE)),
        )
        chart_tab = ft.Container(
            content=ft.Column([
                ft.Row([self.chart_symbol, self.chart_interval,
                        ft.FilledButton("Cargar", icon=ft.Icons.SHOW_CHART,
                                        on_click=self.on_load_chart)],
                       spacing=10, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row([self.chart_last, self.chart_change], spacing=16),
                self.price_chart,
            ], spacing=10, expand=True),
            padding=12, expand=True,
        )

        # --- Historial --------------------------------------------------------
        self.hist_symbol = ft.TextField(
            label="Símbolo (opcional)", width=150, dense=True, text_size=14,
            capitalization=ft.TextCapitalization.CHARACTERS,
        )
        self.hist_days = ft.Dropdown(
            label="Ventana", width=110, value="30", dense=True,
            options=[ft.dropdown.Option(d) for d in ("7", "30", "90", "180")],
        )
        self.hist_only_pos = ft.Switch(label="Solo PnL+", value=True, adaptive=True)
        self.hist_min_pnl = ft.TextField(
            label="PnL mín.", width=100, dense=True, text_size=14,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self.hist_summary = ft.Text("", size=13, color=GREY)
        self.hist_list = ft.ListView(spacing=6, expand=True)
        history_tab = ft.Container(
            content=ft.Column([
                ft.Row([self.hist_symbol, self.hist_days, self.hist_min_pnl],
                       spacing=10, wrap=True),
                ft.Row([self.hist_only_pos,
                        ft.FilledButton("Buscar", icon=ft.Icons.SEARCH,
                                        on_click=self.on_load_history)], spacing=12),
                ft.Divider(height=8), self.hist_summary, self.hist_list,
            ], spacing=8, expand=True),
            padding=12, expand=True,
        )

        self.tabs = ft.Tabs(
            selected_index=0, animation_duration=250, expand=True,
            tabs=[
                ft.Tab(text="Dashboard", icon=ft.Icons.DASHBOARD_ROUNDED, content=dashboard),
                ft.Tab(text="Órdenes", icon=ft.Icons.LIST_ALT_ROUNDED, content=orders_tab),
                ft.Tab(text="Gráfico", icon=ft.Icons.SHOW_CHART_ROUNDED, content=chart_tab),
                ft.Tab(text="Historial", icon=ft.Icons.HISTORY_ROUNDED, content=history_tab),
            ],
        )
        self.controls = [
            ft.Container(
                content=ft.Row([
                    ft.Text("Bot Futuros", size=20, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.PRIMARY),
                    self.env_badge, ft.Container(expand=True), self.run_switch,
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.padding.symmetric(horizontal=16, vertical=10),
            ),
            ft.Container(
                content=ft.Row([self.status_text, ft.Container(expand=True), self.weight_text]),
                padding=ft.padding.symmetric(horizontal=16),
            ),
            ft.Divider(height=1),
            ft.Container(content=self.tabs, expand=True),
        ]

    # --- Utilidades UI --------------------------------------------------------
    def _snack(self, msg: str, error: bool = False):
        self.page.open(ft.SnackBar(
            ft.Text(msg), bgcolor=ft.Colors.RED_900 if error else None, open=True))
        # Persistente bajo los botones: el snackbar se desvanece, esto se queda.
        self.order_status_text.value = ("⚠ " if error else "▸ ") + msg
        self.order_status_text.color = RED if error else GREEN
        self.page.update()

    async def _confirm(self, title: str, body: str) -> bool:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        dlg = ft.AlertDialog(modal=True, title=ft.Text(title), content=ft.Text(body))

        def close(result: bool):
            self.page.close(dlg)
            if not fut.done():
                fut.set_result(result)

        dlg.actions = [
            ft.TextButton("Cancelar", on_click=lambda e: close(False)),
            ft.FilledButton("Confirmar", on_click=lambda e: close(True)),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        self.page.open(dlg)
        return await fut

    def _set_status(self, msg: str, color=GREY):
        self.status_text.value = msg
        self.status_text.color = color
        self.page.update()

    def _update_symbol_hint(self, e=None):
        sym = (self.symbol_field.value or "").strip().upper()
        if sym in self.filters:
            f = self.filters.get(sym)
            self.symbol_hint.value = (f"{sym} · mín notional {f.min_notional} USDT · "
                                      f"step {f.step_size} · tick {f.tick_size}")
            self.symbol_hint.color = GREY
        elif sym:
            self.symbol_hint.value = f"{sym}: no es un símbolo PERPETUAL válido"
            self.symbol_hint.color = RED
        else:
            self.symbol_hint.value = ""
        self.page.update()

    # --- Autocompletado de símbolo + sliders sincronizados -------------------
    def _on_symbol_change(self, e=None):
        self._update_symbol_hint()
        self._build_symbol_suggestions()
        self._recompute_notional_range()

    def _build_symbol_suggestions(self):
        text = (self.symbol_field.value or "").strip().upper()
        self.symbol_suggestions.controls.clear()
        matches = []
        if text and text not in self._all_symbols:
            # prioriza los que EMPIEZAN por el texto, luego los que lo contienen
            starts = [s for s in self._all_symbols if s.startswith(text)]
            contains = [s for s in self._all_symbols if text in s and not s.startswith(text)]
            matches = (starts + contains)[:8]
            for s in matches:
                self.symbol_suggestions.controls.append(ft.Container(
                    content=ft.Text(s, size=13, weight=ft.FontWeight.W_500),
                    data=s, on_click=self._pick_symbol, ink=True, width=320,
                    padding=ft.padding.symmetric(horizontal=12, vertical=8),
                    bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.WHITE), border_radius=6))
        self.symbol_suggestions.visible = bool(matches)
        self.page.update()

    def _pick_symbol(self, e):
        self.symbol_field.value = e.control.data
        self.symbol_suggestions.controls.clear()
        self.symbol_suggestions.visible = False
        self._update_symbol_hint()
        self._recompute_notional_range()
        self.page.run_task(self._fetch_mark_for_preview, e.control.data)
        self.page.update()

    def _on_leverage(self, e):
        self._leverage = int(self.leverage_slider.value)
        self.leverage_label.value = f"{self._leverage}x"
        self._recompute_notional_range()

    def _current_notional(self) -> float:
        try:
            return float(self.notional_field.value or "0")
        except ValueError:
            return float(self.notional_slider.min)

    def _set_notional(self, value, update=True):
        value = max(float(self.notional_slider.min),
                    min(float(value), float(self.notional_slider.max)))
        self._syncing = True
        try:
            self.notional_field.value = f"{value:.2f}"
            self.notional_slider.value = value
        finally:
            self._syncing = False
        if update:
            self._update_preview()

    def _on_notional_field(self, e):
        if self._syncing:
            return
        self._set_notional(self._current_notional())

    def _on_notional_slider(self, e):
        if self._syncing:
            return
        self._set_notional(self.notional_slider.value)

    def _notional_min(self, e):
        self._set_notional(self.notional_slider.min)

    def _notional_max(self, e):
        self._set_notional(self.notional_slider.max)

    def _recompute_notional_range(self):
        """Ajusta el rango del slider de notional al mínimo del símbolo y al saldo×lev."""
        sym = (self.symbol_field.value or "").strip().upper()
        min_n = float(self.filters.get(sym).min_notional) if sym in self.filters else 5.0
        cap = float(self.risk.limits.max_position_notional)
        affordable = self._available_balance * self._leverage
        max_n = min(affordable, cap) if affordable > 0 else cap
        if max_n <= min_n:
            max_n = min_n * 2
        self.notional_slider.min = round(min_n, 2)
        self.notional_slider.max = round(max_n, 2)
        self._set_notional(self._current_notional(), update=False)  # auto-ajuste por mínimo
        self._update_preview()

    def _update_preview(self):
        """Previsualiza costo, liquidación estimada y niveles antes de abrir."""
        lev = max(1, int(self._leverage))
        liq_frac = 1.0 / lev
        if self.auto_switch.value:
            tp_frac, sl_frac = auto_levels(lev, target_roe=AUTO_TARGET_ROE,
                                           stop_roe=AUTO_STOP_ROE)
            tp_pct, sl_pct = tp_frac * 100, sl_frac * 100
            try:
                cost = float(self.margin_field.value or 0)
            except ValueError:
                cost = 0.0
        else:
            try:
                tp_pct = float(self.tp_field.value or 0)
            except ValueError:
                tp_pct = 0.0
            try:
                sl_pct = float(self.stop_field.value or 0)
            except ValueError:
                sl_pct = 0.0
            cost = self._current_notional() / lev
        mark = self._mark_cache.get((self.symbol_field.value or "").strip().upper())
        parts = [f"Costo ≈ {fmt_usd(cost)}", f"Liquidación ≈ ±{liq_frac*100:.1f}% ({lev}x)"]
        if mark:
            parts.append(f"→ LONG {fmt_price(mark*(1-liq_frac))} / "
                         f"SHORT {fmt_price(mark*(1+liq_frac))}")
        parts.append(f"· SL ±{sl_pct:.2f}% · TP +{tp_pct:.2f}%")
        warn = (sl_pct / 100.0) >= liq_frac
        if warn:
            parts.append("⚠ el SL queda FUERA de la liquidación")
        self.liq_preview.value = "  ".join(parts)
        self.liq_preview.color = RED if warn else GREY
        self.page.update()

    async def _fetch_mark_for_preview(self, symbol):
        try:
            mp = await self.client.mark_price(symbol)
            self._mark_cache[symbol] = float(
                (mp[0] if isinstance(mp, list) else mp)["markPrice"])
        except (BinanceError, KeyError):
            return
        self._update_preview()

    def _on_levels_change(self, e=None):
        self._update_preview()

    def _on_auto_toggle(self, e=None):
        self._update_preview()

    # --- Inicialización -------------------------------------------------------
    async def initialize(self):
        if not API_KEY or not SECRET_KEY:
            self._set_status("Sin credenciales en .env", RED)
            return
        try:
            await self.client.sync_time()
            await self.filters.refresh()
            self._all_symbols = sorted(self.filters.symbols())
            self._set_status(f"Conectado · {len(self.filters)} símbolos PERPETUAL", GREEN)
            self._update_symbol_hint()
            await self.refresh()
            self._recompute_notional_range()  # rango con saldo ya conocido
            await self._fetch_mark_for_preview(
                (self.symbol_field.value or "BTCUSDT").strip().upper())
            await self.on_load_market()  # resumen + top (1d, barato)
            await self.on_load_chart()   # carga inicial del gráfico
        except BinanceError as e:
            self._set_status(f"Error: {e}", RED)

    # --- Monitoreo por WebSocket ---------------------------------------------
    def toggle_monitoring(self, e):
        self.is_running = e.control.value
        if self.is_running:
            self.page.run_task(self._start_monitoring)
        else:
            self.page.run_task(self._stop_monitoring)

    async def _start_monitoring(self):
        self._set_status("Monitoreo en vivo (WebSocket)…", GREEN)
        if self._user_stream is None:
            self._user_stream = UserStream(
                self.client, self._on_user_event, on_status=self._on_stream_status)
            self._user_stream.start()
        await self.refresh()
        if self._fallback_task is None or self._fallback_task.done():
            self._fallback_task = asyncio.create_task(self._fallback_loop())

    async def _stop_monitoring(self):
        if self._user_stream:
            await self._user_stream.stop()
            self._user_stream = None
        if self._mark_stream:
            await self._mark_stream.stop()
            self._mark_stream = None
            self._mark_symbols = []
        if self._fallback_task:
            self._fallback_task.cancel()
            self._fallback_task = None
        self._set_status("Monitoreo detenido", GREY)

    async def _fallback_loop(self):
        while self.is_running:
            await asyncio.sleep(FALLBACK_POLL_SECONDS)
            if self.is_running:
                try:
                    await self.refresh()
                except BinanceError:
                    pass

    def _on_stream_status(self, msg: str):
        if "reconect" in msg:
            self._set_status(msg, ft.Colors.AMBER)

    async def _on_user_event(self, data):
        # Coalescer ráfagas (un fill emite ACCOUNT_UPDATE + ORDER_TRADE_UPDATE).
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(self._debounced_refresh())

    async def _debounced_refresh(self):
        await asyncio.sleep(0.4)
        try:
            await self.refresh()
        except BinanceError as e:
            self._set_status(f"Error refresco: {e}", RED)

    async def refresh(self):
        account = await self.client.account()
        positions = [p for p in await self.client.position_risk()
                     if float(p.get("positionAmt", 0)) != 0]
        orders = await self.client.open_orders()
        algo_orders = await self.trader.open_algo_orders()
        self._render_account(account)
        self._render_positions(positions)
        self._render_orders(orders, algo_orders)
        if self.client.rate_limits:
            w = self.client.rate_limits.get("X-MBX-USED-WEIGHT-1M", "?")
            self.weight_text.value = f"peso 1m: {w}"
        try:
            daily = await self.risk.realized_pnl_today()
            limit = self.risk.limits.daily_loss_limit
            breached = daily <= -limit
            self.acct_daily.value = (f"PnL hoy: {fmt_usd(daily)} (límite -{limit})"
                                     + (" · KILL-SWITCH ACTIVO" if breached else ""))
            self.acct_daily.color = RED if (breached or float(daily) < 0) else GREEN
        except BinanceError:
            pass
        self.page.update()
        if self.is_running:
            await self._resubscribe_marks([p["symbol"] for p in positions])

    # --- markPrice en vivo (PnL de posiciones) -------------------------------
    async def _resubscribe_marks(self, symbols):
        symbols = sorted(set(symbols))
        if symbols == self._mark_symbols:
            return
        self._mark_symbols = symbols
        if self._mark_stream:
            await self._mark_stream.stop()
            self._mark_stream = None
        if symbols:
            streams = [f"{s.lower()}@markPrice@1s" for s in symbols]
            self._mark_stream = MarketStream(self.client.ws_base, streams, self._on_mark)
            self._mark_stream.start()

    def _on_mark(self, data):
        sym = data.get("s")
        p = data.get("p")
        st = self._pos_state.get(sym)
        if not st or p is None:
            return
        mark = float(p)
        upnl = (mark - st["entry"]) * st["amt"]  # lineal USDT-M
        st["cur_pnl"] = upnl
        st["mark_text"].value = f"Marca: {mark:,.2f}"
        st["pnl_text"].value = fmt_usd(upnl)
        st["pnl_text"].color = pnl_color(upnl)
        total = sum(s["cur_pnl"] for s in self._pos_state.values())
        self.acct_upnl.value = f"PnL no realizado: {fmt_usd(total)}"
        self.acct_upnl.color = pnl_color(total)
        self.page.update()

    # --- Render ---------------------------------------------------------------
    def _render_account(self, acct: dict):
        margin_balance = float(acct.get("totalMarginBalance", 0) or 0)
        upnl = float(acct.get("totalUnrealizedProfit", 0) or 0)
        avail = float(acct.get("availableBalance", 0) or 0)
        self._available_balance = avail
        maint = float(acct.get("totalMaintMargin", 0) or 0)
        ratio = (maint / margin_balance * 100) if margin_balance else 0.0
        self.acct_balance.value = fmt_usd(margin_balance)
        self.acct_upnl.value = f"PnL no realizado: {fmt_usd(upnl)}"
        self.acct_upnl.color = pnl_color(upnl)
        self.acct_avail.value = f"Disponible: {fmt_usd(avail)}"
        self.acct_margin_ratio.value = f"Margen: {ratio:.2f}%"
        self.acct_margin_ratio.color = RED if ratio >= 80 else GREY

    def _render_positions(self, positions: list):
        self.positions_list.controls.clear()
        self._pos_state = {}
        if not positions:
            self.positions_list.controls.append(
                ft.Text("Sin posiciones abiertas.", italic=True, color=GREY))
            return
        for p in positions:
            symbol = p.get("symbol", "—")
            amt = float(p.get("positionAmt", 0))
            entry = float(p.get("entryPrice", 0))
            is_long = amt > 0
            upnl = float(p.get("unRealizedProfit", 0))
            pnl_text = ft.Text(fmt_usd(upnl), weight=ft.FontWeight.BOLD, color=pnl_color(upnl))
            mark_text = ft.Text(f"Marca: {p.get('markPrice')}", size=12, color=GREY)
            self._pos_state[symbol] = {
                "entry": entry, "amt": amt, "pnl_text": pnl_text,
                "mark_text": mark_text, "cur_pnl": upnl,
            }
            self.positions_list.controls.append(ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(symbol, weight=ft.FontWeight.BOLD, size=16),
                        ft.Container(
                            content=ft.Text("LONG" if is_long else "SHORT", size=10,
                                            weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                            bgcolor=ft.Colors.GREEN_700 if is_long else ft.Colors.RED_700,
                            padding=ft.padding.symmetric(horizontal=8, vertical=2),
                            border_radius=6),
                        ft.Container(expand=True), pnl_text,
                    ]),
                    ft.Text(f"Tam: {abs(amt)} · Entrada: {p.get('entryPrice')} · "
                            f"BE: {p.get('breakEvenPrice')}", size=12, color=GREY),
                    ft.Row([mark_text, ft.Text(
                        f"Liq: {p.get('liquidationPrice')} · Notional: {fmt_usd(p.get('notional'))}",
                        size=12, color=GREY)], spacing=12, wrap=True),
                    ft.Row([ft.OutlinedButton(
                        "Cerrar (MARKET)", icon=ft.Icons.CLOSE,
                        on_click=self.on_close_position,
                        data=(symbol, amt, p.get("positionSide", "BOTH")))],
                        alignment=ft.MainAxisAlignment.END),
                ], spacing=5),
                padding=12, border_radius=10,
                bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.WHITE),
            ))

    def _render_orders(self, orders: list, algo_orders: list):
        self.open_orders_list.controls.clear()
        if not orders and not algo_orders:
            self.open_orders_list.controls.append(
                ft.Text("No hay órdenes abiertas.", italic=True, color=GREY))
            return
        for o in orders:
            self.open_orders_list.controls.append(ft.Container(
                content=ft.Row([
                    ft.Column([
                        ft.Text(f"[{o.get('type')}] {o.get('symbol')}", weight=ft.FontWeight.BOLD),
                        ft.Text(f"{o.get('side')} · {o.get('origQty')} @ {o.get('price')}",
                                size=12, color=GREY),
                    ], expand=True, spacing=2),
                    ft.TextButton("Cancelar", icon=ft.Icons.CANCEL_OUTLINED, icon_color=RED,
                                  on_click=self.on_cancel_order,
                                  data=(o.get("symbol"), o.get("orderId"))),
                ]),
                padding=10, border_radius=8,
                bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.WHITE),
            ))
        # Órdenes condicionales (stop-loss/TP/trailing) viven en el servicio Algo.
        for a in algo_orders:
            atype = a.get("orderType") or a.get("type") or "ALGO"
            closes = a.get("closePosition") in (True, "true")
            detail = (f"{a.get('side')} · trigger {a.get('triggerPrice')} · "
                      + ("cierra todo" if closes else f"qty {a.get('quantity')}"))
            self.open_orders_list.controls.append(ft.Container(
                content=ft.Row([
                    ft.Column([
                        ft.Text(f"🛡 [{atype}] {a.get('symbol')}", weight=ft.FontWeight.BOLD,
                                color=ft.Colors.AMBER_200),
                        ft.Text(detail, size=12, color=GREY),
                    ], expand=True, spacing=2),
                    ft.TextButton("Cancelar", icon=ft.Icons.CANCEL_OUTLINED, icon_color=RED,
                                  on_click=self.on_cancel_algo,
                                  data=a.get("algoId")),
                ]),
                padding=10, border_radius=8,
                bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.AMBER),
            ))

    # --- Gráfico --------------------------------------------------------------
    async def on_load_chart(self, e=None):
        symbol = (self.chart_symbol.value or "").strip().upper()
        interval = self.chart_interval.value or "1h"
        if symbol not in self.filters:
            self._snack(f"Símbolo {symbol} no válido.", error=True)
            return
        if self._chart_stream:
            await self._chart_stream.stop()
            self._chart_stream = None
        try:
            kl = await self.client.klines(symbol, interval, limit=CHART_POINTS)
            t = await self.client.ticker_24h(symbol)
        except BinanceError as ex:
            self._snack(f"Error klines: {ex}", error=True)
            return
        self._chart_closes = [float(k[4]) for k in kl]
        self._chart_last_open = int(kl[-1][0]) if kl else None
        self._chart_symbol_active = symbol
        self._render_chart()
        chg = float(t.get("priceChangePercent", 0) or 0)
        self.chart_last.value = f"{symbol}  {float(t.get('lastPrice', 0) or 0):,.2f}"
        self.chart_change.value = f"24h: {chg:+.2f}%"
        self.chart_change.color = pnl_color(chg)
        self.page.update()
        self._chart_stream = MarketStream(
            self.client.ws_base, [f"{symbol.lower()}@kline_{interval}"], self._on_chart_kline)
        self._chart_stream.start()

    def _render_chart(self):
        closes = self._chart_closes
        if not closes:
            self.price_chart.data_series = []
            return
        lo, hi = min(closes), max(closes)
        pad = (hi - lo) * 0.05 or (hi * 0.001 + 1)
        self.price_chart.min_y = lo - pad
        self.price_chart.max_y = hi + pad
        self.price_chart.min_x = 0
        self.price_chart.max_x = len(closes) - 1
        self.price_chart.data_series = [ft.LineChartData(
            data_points=[ft.LineChartDataPoint(i, c) for i, c in enumerate(closes)],
            stroke_width=2, color=ft.Colors.CYAN_300, curved=True, stroke_cap_round=True,
        )]

    def _on_chart_kline(self, data):
        k = data.get("k", {})
        close = float(k.get("c", 0) or 0)
        kt = k.get("t")
        if not self._chart_closes or kt is None:
            return
        if self._chart_last_open is None or kt == self._chart_last_open:
            self._chart_closes[-1] = close
            self._chart_last_open = kt
        elif kt > self._chart_last_open:
            self._chart_closes.append(close)
            self._chart_last_open = kt
            if len(self._chart_closes) > CHART_POINTS:
                self._chart_closes.pop(0)
        self._render_chart()
        if self._chart_symbol_active:
            self.chart_last.value = f"{self._chart_symbol_active}  {close:,.2f}"
        self.page.update()

    # --- Mercado: resumen + Top movers ---------------------------------------
    async def on_load_market(self, e=None):
        window = self.market_tf.value or "1d"
        topn = int(self.market_topn.value or "10")
        universe = max(topn, 50)
        self.market_summary.value = "Cargando mercado…"
        self.market_list.controls.clear()
        self.page.update()
        try:
            tickers = await market_mod.fetch_tickers(
                self.client, allowed=self.filters.symbols())
            summary = market_mod.summarize_market(tickers)
            movers = await market_mod.top_movers(
                self.client, tickers, window=window, limit=topn, universe=universe)
        except BinanceError as ex:
            self.market_summary.value = f"Error mercado: {ex}"
            self.page.update()
            return
        vol_b = summary.total_quote_volume / 1e9
        txt = (f"▲ {summary.advancers} / ▼ {summary.decliners} · "
               f"Vol 24h ${vol_b:,.1f}B · prom {summary.avg_change:+.2f}%")
        if summary.top_gainer:
            txt += f" · 🔼 {summary.top_gainer.symbol} {summary.top_gainer.change_pct:+.1f}%"
        if summary.top_loser:
            txt += f" · 🔽 {summary.top_loser.symbol} {summary.top_loser.change_pct:+.1f}%"
        self.market_summary.value = txt
        self.market_summary.color = pnl_color(summary.avg_change)
        self._render_movers(movers)
        self.page.update()

    def _render_movers(self, movers):
        self.market_list.controls.clear()
        if not movers:
            self.market_list.controls.append(
                ft.Text("Sin datos.", italic=True, color=GREY))
            return
        for m in movers:
            self.market_list.controls.append(ft.Container(
                content=ft.Row([
                    ft.Text(m.symbol, weight=ft.FontWeight.BOLD, size=13, width=130),
                    ft.Text(fmt_price(m.last), size=12, color=GREY, expand=True),
                    ft.Text(f"{m.change_pct:+.2f}%", size=13, weight=ft.FontWeight.BOLD,
                            color=pnl_color(m.change_pct)),
                ]),
                on_click=self.on_mover_click, data=m.symbol, ink=True,
                padding=ft.padding.symmetric(horizontal=8, vertical=6), border_radius=6,
            ))

    async def on_mover_click(self, e):
        sym = e.control.data
        self.chart_symbol.value = sym
        self.tabs.selected_index = 2  # pestaña Gráfico
        self.page.update()
        await self.on_load_chart()

    # --- Acciones de trading --------------------------------------------------
    async def _open_position(self, side: str):
        symbol = (self.symbol_field.value or "").strip().upper()
        try:
            notional = Decimal(str(self.notional_field.value or "0"))
            tp_pct = float(self.tp_field.value or "0")
            sl_pct = float(self.stop_field.value or "0")
            max_hold_min = float(self.maxhold_field.value or "0")
        except Exception:
            self._snack("Valores numéricos inválidos.", error=True)
            return
        if tp_pct <= 0 or sl_pct <= 0:
            self._snack("Take-profit y stop-loss son obligatorios (> 0%).", error=True)
            return
        if symbol not in self.filters:
            self._snack(f"Símbolo {symbol} no válido.", error=True)
            return
        sfilt = self.filters.get(symbol)
        if notional < sfilt.min_notional:
            self._snack(
                f"Notional {notional} < mínimo de {symbol} ({sfilt.min_notional} USDT). "
                f"Sube el notional o usa un símbolo con mínimo menor.", error=True)
            return
        try:
            await self.risk.check_open(symbol, notional)
        except RiskError as re:
            self._snack(f"Bloqueado por riesgo: {re}", error=True)
            return
        try:
            mp = await self.client.mark_price(symbol)
            mark = Decimal(str((mp[0] if isinstance(mp, list) else mp)["markPrice"]))
            adj_qty = sfilt.format_qty(notional / mark, market=True)
        except (BinanceError, KeyError) as e:
            self._snack(f"No se pudo calcular cantidad: {e}", error=True)
            return
        lev = min(self._leverage, self.risk.limits.max_leverage)
        hold_s = int(max_hold_min * 60)
        maker = bool(self.maker_switch.value)
        entry_kind = "LÍMITE/maker" if maker else "MARKET/taker"
        ok = await self._confirm(
            f"Scalp {side} {symbol}",
            f"{entry_kind} {side} · ~{fmt_usd(notional)} ({adj_qty}) a ~{mark} · {lev}x\n"
            f"TP +{tp_pct}% · SL -{sl_pct}% · cierre auto en {max_hold_min:g} min."
            + ("  [TESTNET]" if TESTNET else "  ⚠ CUENTA REAL"))
        if not ok:
            return
        cfg = ScalpConfig(
            notional=notional, take_profit_pct=tp_pct / 100.0,
            stop_loss_pct=sl_pct / 100.0, max_hold_seconds=hold_s, leverage=lev,
            entry_type="MAKER" if maker else "MARKET", entry_timeout_s=30)
        try:
            res = await open_scalp(self.trader, self.client, symbol=symbol, side=side,
                                   config=cfg, mark_price=mark)
        except BinanceError as e:
            self._snack(f"Fallo al abrir scalp: {e}", error=True)
            return
        if not res.get("filled", True):
            self._snack(f"Entrada límite de {symbol} no se llenó a tiempo; cancelada.")
            await self.refresh()
            return
        if res["errors"]:
            self._snack(f"⚠ Scalp abierto SIN bracket completo: {res['errors']}. "
                        f"Cierre por tiempo sigue activo.", error=True)
        else:
            self._snack(f"Scalp {symbol} {side} ({entry_kind}): TP +{tp_pct}% / "
                        f"SL -{sl_pct}% · cierre auto {max_hold_min:g} min")
        sl_id = res["sl"].get("algoId") if res.get("sl") else None
        self.page.run_task(self._trailing_task, symbol, side, float(mark),
                           res.get("sl_trigger"), sl_id, tp_pct / 100.0, hold_s)
        await self.refresh()

    async def _trailing_task(self, symbol, side, entry, initial_stop, sl_id, tp_frac, hold_s):
        """Mueve el SL a break-even y luego trailing; cierra por tiempo al expirar."""
        if not entry or hold_s <= 0:
            return
        be_trigger = max(tp_frac * 0.5, 0.002)  # a positivo a mitad de camino del TP
        try:
            await manage_trailing_stop(
                self.trader, self.client, symbol=symbol, side=side, entry=entry,
                initial_stop=initial_stop, sl_algo_id=sl_id,
                be_trigger_pct=be_trigger, trail_pct=TRAIL_PCT, max_hold_seconds=hold_s)
            await self.refresh()
        except BinanceError:
            pass

    async def _auto_open(self, side: str):
        """Modo Auto: valida/elige símbolo, calcula TP/SL/cantidad y abre con trailing."""
        lev = min(self._leverage, self.risk.limits.max_leverage)
        try:
            margin = Decimal(str(self.margin_field.value or "0"))
        except Exception:
            self._snack("Margen inválido.", error=True)
            return
        if margin <= 0:
            self._snack("El margen debe ser > 0.", error=True)
            return
        tp_frac, sl_frac = auto_levels(lev, target_roe=AUTO_TARGET_ROE, stop_roe=AUTO_STOP_ROE)
        notional = autoscalp.auto_notional(margin, lev)
        typed = (self.symbol_field.value or "").strip().upper()
        valid = self.filters.symbols()
        symbol = None
        try:
            if typed and typed in self.filters:
                t = await self.client.ticker_24h(typed)
                rr = await autoscalp.recent_return_pct(self.client, typed)
                ok, reason = momentum_ok(
                    side=side, last_price=float(t["lastPrice"]),
                    change_pct_24h=float(t["priceChangePercent"]),
                    recent_return_pct=rr, max_price=AUTO_MAX_PRICE)
                if not ok:
                    self._snack(f"{typed} no apta para {side}: {reason}", error=True)
                    return
                symbol = typed
            else:
                tickers = await market_mod.fetch_tickers(self.client, allowed=valid)
                held = {p["symbol"] for p in await self.client.position_risk()
                        if float(p.get("positionAmt", 0)) != 0}
                cands = select_candidates(
                    tickers, valid, max_price=AUTO_MAX_PRICE, min_quote_volume=AUTO_MIN_VOL,
                    top_n=8, exclude=held, direction=side)
                for cand in cands:
                    rr = await autoscalp.recent_return_pct(self.client, cand)
                    if (side == "BUY" and rr > 0) or (side == "SELL" and rr < 0):
                        symbol = cand
                        break
                if not symbol:
                    self._snack("Sin candidatos con fuerza 1m ahora mismo.", error=True)
                    return
        except BinanceError as e:
            self._snack(f"Error analizando mercado: {e}", error=True)
            return

        sf = self.filters.get(symbol)
        if notional < sf.min_notional:
            self._snack(f"Notional {notional} < mínimo de {symbol} ({sf.min_notional}). "
                        f"Sube el margen o el apalancamiento.", error=True)
            return
        try:
            await self.risk.check_open(symbol, notional)
        except RiskError as re:
            self._snack(f"Bloqueado por riesgo: {re}", error=True)
            return
        try:
            mp = await self.client.mark_price(symbol)
            mark = Decimal(str((mp[0] if isinstance(mp, list) else mp)["markPrice"]))
        except (BinanceError, KeyError) as e:
            self._snack(f"No se pudo leer precio: {e}", error=True)
            return

        maker = bool(self.maker_switch.value)
        try:
            hold_s = int(float(self.maxhold_field.value or DEFAULT_MAX_HOLD_MIN) * 60)
        except ValueError:
            hold_s = int(DEFAULT_MAX_HOLD_MIN * 60)
        ok = await self._confirm(
            f"AUTO {side} {symbol}",
            f"{'LÍMITE/maker' if maker else 'MARKET'} · margen {fmt_usd(margin)} × {lev}x "
            f"= {fmt_usd(notional)}\nTP +{tp_frac*100:.2f}% (≈{AUTO_TARGET_ROE*100:.0f}% ROE) · "
            f"SL -{sl_frac*100:.2f}% + trailing a positivo · cierre {hold_s//60} min."
            + ("  [TESTNET]" if TESTNET else "  ⚠ CUENTA REAL"))
        if not ok:
            return
        cfg = ScalpConfig(
            notional=notional, take_profit_pct=tp_frac, stop_loss_pct=sl_frac,
            max_hold_seconds=hold_s, leverage=lev,
            entry_type="MAKER" if maker else "MARKET", entry_timeout_s=15)
        try:
            res = await open_scalp(self.trader, self.client, symbol=symbol, side=side,
                                   config=cfg, mark_price=mark)
        except BinanceError as e:
            self._snack(f"Fallo al abrir: {e}", error=True)
            return
        if not res.get("filled"):
            self._snack(f"Entrada límite de {symbol} no se llenó; cancelada.")
            await self.refresh()
            return
        if res["errors"]:
            self._snack(f"⚠ {symbol} abierto SIN bracket completo: {res['errors']}", error=True)
        else:
            self._snack(f"AUTO {side} {symbol}: TP +{tp_frac*100:.2f}% / "
                        f"SL -{sl_frac*100:.2f}% + trailing")
        sl_id = res["sl"].get("algoId") if res.get("sl") else None
        self.page.run_task(self._trailing_task, symbol, side, float(mark),
                           res.get("sl_trigger"), sl_id, tp_frac, hold_s)
        await self.refresh()

    async def _scalp_timeout(self, symbol: str, seconds: int):
        """Red de seguridad: cierra el scalp si sigue abierto pasado el tiempo máximo."""
        await asyncio.sleep(seconds)
        try:
            if await close_scalp(self.trader, self.client, symbol):
                self._snack(f"{symbol}: cerrado por tiempo ({seconds // 60} min)")
                await self.refresh()
        except BinanceError as e:
            self._snack(f"Error en cierre por tiempo de {symbol}: {e}", error=True)

    async def on_open_long(self, e):
        await self._safe_open("BUY")

    async def on_open_short(self, e):
        await self._safe_open("SELL")

    async def _safe_open(self, side: str):
        """Punto único de apertura: feedback inmediato y NINGÚN fallo silencioso."""
        self._snack(f"Procesando {'LONG' if side == 'BUY' else 'SHORT'}…")
        try:
            if self.auto_switch.value:
                await self._auto_open(side)
            else:
                await self._open_position(side)
        except Exception as ex:  # noqa: BLE001 - todo fallo debe verse
            self._snack(f"No se pudo abrir: {ex}", error=True)

    async def on_close_position(self, e):
        symbol, amt, position_side = e.control.data
        close_side = "SELL" if amt > 0 else "BUY"
        qty = abs(amt)
        if not await self._confirm(f"Cerrar {symbol}",
                                   f"MARKET {close_side} {qty} para cerrar la posición."):
            return
        try:
            res = await self.trader.close_market(symbol, close_side, qty,
                                                 position_side=position_side)
            self._snack(f"Cierre {res.get('status')} · id {res.get('orderId')}")
            await self.refresh()
        except BinanceError as ex:
            self._snack(f"Fallo al cerrar: {ex}", error=True)

    async def on_cancel_order(self, e):
        symbol, order_id = e.control.data
        try:
            res = await self.trader.cancel_order(symbol, order_id=order_id)
            self._snack(f"Orden {order_id} {res.get('status')}.")
            await self.refresh()
        except BinanceError as ex:
            self._snack(f"Error al cancelar: {ex}", error=True)

    async def on_cancel_algo(self, e):
        algo_id = e.control.data
        try:
            await self.trader.cancel_algo_order(algo_id=algo_id)
            self._snack(f"Orden condicional {algo_id} cancelada.")
            await self.refresh()
        except BinanceError as ex:
            self._snack(f"Error al cancelar condicional: {ex}", error=True)

    # --- Historial PnL --------------------------------------------------------
    async def on_load_history(self, e):
        symbol = (self.hist_symbol.value or "").strip().upper() or None
        days = int(self.hist_days.value or "30")
        only_pos = bool(self.hist_only_pos.value)
        min_pnl = None
        if (self.hist_min_pnl.value or "").strip():
            try:
                min_pnl = float(self.hist_min_pnl.value)
            except ValueError:
                self._snack("PnL mín. inválido.", error=True)
                return
        self.hist_summary.value = "Cargando…"
        self.hist_list.controls.clear()
        self.page.update()
        try:
            events = await pnl_mod.fetch_realized_pnl(self.client, symbol=symbol, days=days)
        except BinanceError as ex:
            self.hist_summary.value = f"Error: {ex}"
            self.page.update()
            return
        events = pnl_mod.filter_events(events, only_positive=only_pos, min_pnl=min_pnl)
        s = pnl_mod.summarize(events)
        pf = "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
        self.hist_summary.value = (
            f"{s.count} ops · ganadoras {s.wins} ({s.win_rate*100:.0f}%) · "
            f"total {fmt_usd(s.total)} · PF {pf} · mejor {fmt_usd(s.best)} / peor {fmt_usd(s.worst)}")
        self.hist_summary.color = pnl_color(s.total)
        if not events:
            self.hist_list.controls.append(
                ft.Text("Sin operaciones para esos filtros.", italic=True, color=GREY))
        for ev in events[:300]:
            self.hist_list.controls.append(ft.Container(
                content=ft.Row([
                    ft.Column([
                        ft.Text(ev.symbol, weight=ft.FontWeight.BOLD, size=14),
                        ft.Text(ev.time.astimezone().strftime("%Y-%m-%d %H:%M"),
                                size=11, color=GREY),
                    ], expand=True, spacing=2),
                    ft.Text(fmt_usd(ev.pnl), weight=ft.FontWeight.BOLD, color=pnl_color(ev.pnl)),
                ]),
                padding=10, border_radius=8,
                bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.WHITE),
            ))
        self.page.update()

    # --- Limpieza -------------------------------------------------------------
    async def shutdown(self):
        for s in (self._user_stream, self._mark_stream, self._chart_stream):
            if s:
                await s.stop()
        if self._fallback_task:
            self._fallback_task.cancel()
        await self.client.close()


# Clave de acceso para despliegues web (Render). Si NO está definida (uso local
# en tu Mac), la app abre directo, como siempre.
ACCESS_KEY = os.getenv("APP_ACCESS_KEY")


def access_key_ok(provided: str) -> bool:
    """Compara en tiempo constante. Sin APP_ACCESS_KEY configurada -> acceso libre."""
    if not ACCESS_KEY:
        return True
    return _secrets.compare_digest((provided or "").strip(), ACCESS_KEY)


async def _launch_app(page: ft.Page):
    client = BinanceFuturesClient(API_KEY, SECRET_KEY, testnet=TESTNET)
    audit = AuditLog(PROJECT_ROOT / "logs" / "orders.jsonl")
    app = TradingApp(page, client, audit=audit)

    async def on_disconnect(e):
        await app.shutdown()

    page.on_disconnect = on_disconnect
    page.add(ft.SafeArea(ft.Container(content=app, expand=True), expand=True))
    await app.initialize()


async def main(page: ft.Page):
    page.title = "Bot Flet Futuros"
    page.theme_mode = ft.ThemeMode.DARK
    page.adaptive = True
    page.padding = 0

    if not ACCESS_KEY:
        await _launch_app(page)
        return

    # --- Pantalla de acceso (la app queda expuesta en una URL pública) ---
    key_field = ft.TextField(label="Clave de acceso", password=True,
                             can_reveal_password=True, width=280,
                             on_submit=lambda e: None)
    error_text = ft.Text("", color=ft.Colors.RED_ACCENT_200, size=12)

    async def try_enter(e=None):
        if access_key_ok(key_field.value):
            page.clean()
            await _launch_app(page)
        else:
            await asyncio.sleep(0.8)  # frena fuerza bruta
            error_text.value = "Clave incorrecta."
            key_field.value = ""
            page.update()

    key_field.on_submit = try_enter
    page.add(ft.SafeArea(ft.Container(
        content=ft.Column([
            ft.Text("Bot Futuros", size=24, weight=ft.FontWeight.BOLD),
            ft.Text("Acceso restringido", size=12, color=ft.Colors.GREY_400),
            key_field,
            ft.FilledButton("Entrar", icon=ft.Icons.LOCK_OPEN, on_click=try_enter),
            error_text,
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=14),
        alignment=ft.alignment.center, expand=True,
    ), expand=True))


if __name__ == "__main__":
    _port = os.getenv("PORT")
    if _port:  # despliegue web (Render define PORT)
        ft.app(target=main, view=None, host="0.0.0.0", port=int(_port))
    else:      # uso local de siempre (desktop)
        ft.app(target=main)
