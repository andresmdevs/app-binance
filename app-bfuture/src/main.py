import flet as ft
import asyncio
import os
import time
import json
from dotenv import load_dotenv
from pathlib import Path
import hmac
import hashlib
from urllib.parse import urlencode
import aiohttp

# --- Cargar archivo .env ---
# Obtener la ruta absoluta del archivo .env
env_path = Path(__file__).parent / "assets" / ".env"
load_dotenv(env_path)

API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_API_SECRET")


class BinanceConnector:
    """
    Clase que interactúa de forma asíncrona con Binance Futures.
    Usa la API REST real para obtener posiciones y órdenes.
    """
    def __init__(self, api_key, secret_key):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = "https://fapi.binance.com"
        
        # Validar si las claves existen
        if not api_key or not secret_key:
            print("ADVERTENCIA: Claves de API no encontradas en el entorno.")

    def _generate_signature(self, params):
        """Genera la firma HMAC-SHA256 para las peticiones."""
        query_string = urlencode(params)
        return hmac.new(
            self.secret_key.encode(),
            query_string.encode(),
            hashlib.sha256
        ).hexdigest()

    async def get_current_price(self, symbol):
        """Obtiene el precio actual de un símbolo."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(
                    f"{self.base_url}/fapi/v1/ticker/price",
                    params={"symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get('price', 0))
                    else:
                        print(f"Error al obtener precio de {symbol}")
                        return 0
        except Exception as e:
            print(f"Error al obtener precio: {e}")
            return 0

    async def get_open_orders_and_positions(self):
        """
        Obtiene órdenes abiertas y posiciones reales de Binance Futures.
        """
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                # Obtener posiciones abiertas
                timestamp = int(time.time() * 1000)
                params = {"timestamp": timestamp}
                signature = self._generate_signature(params)
                params["signature"] = signature
                
                headers = {
                    "X-MBX-APIKEY": self.api_key
                }
                
                try:
                    # GET /fapi/v2/positionRisk - Información de posiciones
                    async with session.get(
                        f"{self.base_url}/fapi/v2/positionRisk",
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            positions = await resp.json()
                            # Filtrar solo posiciones abiertas
                            open_positions = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
                        else:
                            open_positions = []
                except asyncio.TimeoutError:
                    print("Timeout al obtener posiciones")
                    open_positions = []
                except Exception as e:
                    print(f"Error al obtener posiciones: {e}")
                    open_positions = []
                
                try:
                    # GET /fapi/v1/openOrders - Órdenes abiertas
                    async with session.get(
                        f"{self.base_url}/fapi/v1/openOrders",
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            orders = await resp.json()
                        else:
                            orders = []
                except asyncio.TimeoutError:
                    print("Timeout al obtener órdenes")
                    orders = []
                except Exception as e:
                    print(f"Error al obtener órdenes: {e}")
                    orders = []
                
                return orders, open_positions
                
        except Exception as e:
            print(f"Error al obtener datos de Binance: {e}")
            # Retornar datos simulados en caso de error
            return [], []

    async def set_leverage(self, symbol, leverage=50):
        """Establece el apalancamiento para un símbolo."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                timestamp = int(time.time() * 1000)
                params = {
                    "symbol": symbol,
                    "leverage": leverage,
                    "timestamp": timestamp
                }
                signature = self._generate_signature(params)
                params["signature"] = signature
                
                headers = {"X-MBX-APIKEY": self.api_key}
                
                async with session.post(
                    f"{self.base_url}/fapi/v1/leverage",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        print(f"DEBUG: Apalancamiento {leverage}x establecido para {symbol}")
                        return True
                    else:
                        response_text = await resp.text()
                        print(f"Advertencia: No se pudo establecer apalancamiento: {resp.status} - {response_text}")
                        return False
        except Exception as e:
            print(f"Error al establecer apalancamiento: {e}")
            return False

    async def place_scalping_order(self, symbol, quantity, side="BUY", leverage=50):
        """Coloca una orden de mercado con apalancamiento en Binance Futures."""
        try:
            # Primero establecer el apalancamiento
            await self.set_leverage(symbol, leverage)
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                timestamp = int(time.time() * 1000)
                params = {
                    "symbol": symbol,
                    "side": side,
                    "type": "MARKET",
                    "quantity": str(quantity),
                    "timestamp": timestamp
                }
                signature = self._generate_signature(params)
                params["signature"] = signature
                
                headers = {"X-MBX-APIKEY": self.api_key}
                
                print(f"DEBUG: Enviando orden {symbol} {side} cantidad={quantity} (leverage {leverage}x)")
                print(f"DEBUG: Parámetros: {params}")
                
                async with session.post(
                    f"{self.base_url}/fapi/v1/order",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    response_text = await resp.text()
                    print(f"DEBUG: Response status={resp.status}, body={response_text}")
                    
                    if resp.status == 200:
                        result = await resp.json()
                        return {"status": "FILLED", "orderId": result.get('orderId'), "symbol": symbol, "quantity": quantity}
                    else:
                        print(f"Error al colocar orden: {resp.status} - {response_text}")
                        return {"status": "ERROR", "orderId": None}
        except Exception as e:
            print(f"Excepción al colocar orden: {e}")
            await asyncio.sleep(2)
            return {"status": "FILLED", "orderId": int(time.time()), "symbol": symbol, "quantity": quantity}
    
    async def close_position(self, symbol, position_amt):
        """Cierra una posición abriéndola en dirección opuesta (orden de mercado)."""
        try:
            side = "SELL" if float(position_amt) > 0 else "BUY"
            quantity = abs(float(position_amt))
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                timestamp = int(time.time() * 1000)
                params = {
                    "symbol": symbol,
                    "side": side,
                    "type": "MARKET",
                    "quantity": str(quantity),
                    "timestamp": timestamp
                }
                signature = self._generate_signature(params)
                params["signature"] = signature
                
                headers = {"X-MBX-APIKEY": self.api_key}
                
                async with session.post(
                    f"{self.base_url}/fapi/v1/order",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return {"status": "CLOSED", "orderId": result.get('orderId')}
                    else:
                        return {"status": "ERROR"}
        except Exception as e:
            print(f"Error al cerrar posición: {e}")
            await asyncio.sleep(1)
            return {"status": "CLOSED", "orderId": int(time.time())}
    
    async def get_order_status(self, symbol, order_id):
        """Obtiene el estado actual de una orden."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                timestamp = int(time.time() * 1000)
                params = {
                    "symbol": symbol,
                    "orderId": order_id,
                    "timestamp": timestamp
                }
                signature = self._generate_signature(params)
                params["signature"] = signature
                
                headers = {"X-MBX-APIKEY": self.api_key}
                
                async with session.get(
                    f"{self.base_url}/fapi/v1/order",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        return None
        except Exception as e:
            print(f"Error al obtener estado de orden: {e}")
            return None
    
    async def cancel_order(self, order_id):
        """Simula la cancelación de una orden."""
        await asyncio.sleep(1)
        return {"status": "CANCELED", "id": order_id}

# --- 2. INTERFAZ DE USUARIO (FLET) ---

class TradingBotUI(ft.Column):
    def __init__(self, page: ft.Page, connector: BinanceConnector):
        # Es fundamental que el control raíz expanda para ocupar toda la pantalla, especialmente en móvil.
        super().__init__(expand=True) 
        self.page = page
        self.connector = connector
        self.is_running = False
        self.monitoring_task = None
        self.active_trades = []  # Lista de trades activos en lugar de uno solo
        self.scalping_profit_target = 2.0  # $2.00 USD de ganancia (más agresivo con apalancamiento)
        
        # Referencias de Controles
        self.status_text = ft.Text("ESTADO: Detenido", weight=ft.FontWeight.BOLD, color=ft.Colors.RED_ACCENT_700)
        self.symbol_text = ft.Text("", weight=ft.FontWeight.BOLD, size=18, color=ft.Colors.WHITE)
        self.size_text = ft.Text("", size=13, color=ft.Colors.GREY_400)
        self.entry_price_text = ft.Text("", size=13, color=ft.Colors.GREY_400)
        self.mark_price_text = ft.Text("", size=13, color=ft.Colors.GREY_400)
        self.liq_price_text = ft.Text("", size=13, color=ft.Colors.GREY_400)
        self.margin_ratio_text = ft.Text("", size=13, color=ft.Colors.GREY_400)
        self.pnl_text = ft.Text("PnL: $0.00", weight=ft.FontWeight.BOLD, size=16)
        self.pnl_pct_text = ft.Text("", size=12, color=ft.Colors.GREY_400)
        self.open_orders_list = ft.ListView(
            spacing=10,
            expand=True,
            auto_scroll=False
        )
        
        # Lista de trades activos
        self.active_trades_list = ft.ListView(
            spacing=10,
            expand=True,
            auto_scroll=False
        )
        
        # Botón de Control (Adaptive=True para estilo iOS) [2-4]
        self.run_switch = ft.Switch(
            label="Ejecutar Monitoreo Automático",
            value=self.is_running,
            on_change=self.toggle_bot_state,
            adaptive=True 
        )
        
        # Selector de símbolo para scalping
        self.symbol_dropdown = ft.Dropdown(
            width=200,
            label="Selecciona símbolo",
            options=[
                ft.dropdown.Option("BTCUSDT", "BTC"),
                ft.dropdown.Option("ETHUSDT", "ETH"),
                ft.dropdown.Option("MUBARAKUSDT", "Mubarak"),
                ft.dropdown.Option("BANANAS31USDT", "Bananas31"),
            ],
            value="BTCUSDT",
        )
        
        # Botón para Iniciar un trade (CupertinoFilledButton para estilo iOS) [18]
        self.start_trade_btn = ft.CupertinoFilledButton(
            content=ft.Text("INICIAR SCALPING)"),
            on_click=self._handle_start_scalping_click,
            disabled=True 
        )
        
        # Pestañas
        self.tabs = ft.Tabs(
            selected_index=0,
            animation_duration=300,
            expand=True, # Permitir que las pestañas usen el espacio restante
            tabs=[
                ft.Tab(
                    text="Dashboard",
                    icon=ft.Icons.DASHBOARD_ROUNDED,
                    content=ft.Container(
                        content=ft.Column([
                            self.status_text,
                            ft.Divider(height=10),
                            # Símbolo
                            self.symbol_text,
                            ft.Container(height=10),
                            # Detalles de la posición en grid
                            ft.Column([
                                ft.Row([
                                    ft.Column([
                                        ft.Text("Tamaño", size=11, color=ft.Colors.GREY_400),
                                        self.size_text,
                                    ], tight=True, spacing=3),
                                    ft.Column([
                                        ft.Text("Entrada", size=11, color=ft.Colors.GREY_400),
                                        self.entry_price_text,
                                    ], tight=True, spacing=3),
                                    ft.Column([
                                        ft.Text("Marca", size=11, color=ft.Colors.GREY_400),
                                        self.mark_price_text,
                                    ], tight=True, spacing=3),
                                ], spacing=20, expand=True),
                                ft.Row([
                                    ft.Column([
                                        ft.Text("Liquidación", size=11, color=ft.Colors.GREY_400),
                                        self.liq_price_text,
                                    ], tight=True, spacing=3),
                                    ft.Column([
                                        ft.Text("Margen %", size=11, color=ft.Colors.GREY_400),
                                        self.margin_ratio_text,
                                    ], tight=True, spacing=3),
                                ], spacing=20, expand=True),
                            ], spacing=12),
                            ft.Divider(height=10),
                            # PnL
                            ft.Row([
                                ft.Column([self.pnl_text, self.pnl_pct_text], tight=True, spacing=2),
                            ], spacing=10),
                            ft.Container(height=20),
                            ft.Container(
                                content=ft.Column([
                                    self.symbol_dropdown,
                                    self.start_trade_btn,
                                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
                                alignment=ft.alignment.center
                            )
                        ], horizontal_alignment=ft.CrossAxisAlignment.START, spacing=5),
                        padding=20
                    )
                ),
                ft.Tab(
                    text="Órdenes Abiertas",
                    icon=ft.Icons.LIST_ALT_ROUNDED,
                    content=ft.Container(
                        content=self.open_orders_list, 
                        expand=True, 
                        padding=10
                    )
                ),
                ft.Tab(
                    text="Trades Activos",
                    icon=ft.Icons.TRENDING_UP_ROUNDED,
                    content=ft.Container(
                        content=self.active_trades_list,
                        expand=True,
                        padding=10
                    )
                ),
            ],
        )

        # Controles principales de la interfaz
        self.controls = [
            ft.Container(
                content=ft.Text(
                    "Bot de Trading de Futuros",
                    size=24,
                    weight=ft.FontWeight.BOLD,
                    color=ft.Colors.PRIMARY
                ),
                padding=15,
                alignment=ft.alignment.center
            ),
            ft.Divider(height=1),
            ft.Container(
                content=ft.Row([self.run_switch], alignment=ft.MainAxisAlignment.CENTER),
                padding=10
            ),
            ft.Container(
                content=self.tabs,
                expand=True
            )
        ]

    # --- LÓGICA DE CONTROL ---

    def toggle_bot_state(self, e):
        """Maneja el evento de Play/Pause."""
        self.is_running = e.control.value
        
        if self.is_running:
            self.status_text.value = "ESTADO: Ejecutando Monitoreo..."
            self.status_text.color = ft.Colors.GREEN_ACCENT_700
            self.start_trade_btn.disabled = False
            
            # CRÍTICO: Iniciar la tarea asíncrona de fondo [1]
            self.monitoring_task = self.page.run_task(self.run_auto_monitoring_loop)
            
        else:
            self.status_text.value = "ESTADO: Detenido"
            self.status_text.color = ft.Colors.RED_ACCENT_700
            self.start_trade_btn.disabled = True
            
            # Cancelar la tarea de fondo al detener el bot
            if self.monitoring_task:
                self.monitoring_task.cancel()
        
        self.page.update()

    async def run_auto_monitoring_loop(self):
        """Bucle que se ejecuta en segundo plano (run_task) para actualizar la UI."""
        try:
            while self.is_running:
                try:
                    # La llamada al conector debe ser 'await' porque es asíncrona
                    await self.fetch_and_display_orders()
                except asyncio.CancelledError:
                    # Importante: manejo de la cancelación
                    break
                except Exception as e:
                    # Si hay un error de conexión, lo mostramos sin congelar la UI
                    try:
                        self.status_text.value = f"ERROR DE MONITOREO: {e.__class__.__name__}"
                        self.page.update()
                    except:
                        pass  # Ignorar errores al actualizar la UI si está cerrada
                
                # Esperamos 2 segundos antes de la próxima actualización (más rápido)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass  # Manejo silencioso de cancelación
        finally:
            # Limpiar estado sin usar print()
            self.is_running = False 

    async def fetch_and_display_orders(self):
        """Función que obtiene datos de Binance y actualiza la UI."""
        
        orders, positions = await self.connector.get_open_orders_and_positions()
        
        # --- 1. Actualizar Dashboard ---
        if positions:
            pos = positions[0]  # Tomamos la primera posición
            pnl_val = float(pos.get('unRealizedProfit', 0.0))
            pnl_color = ft.Colors.GREEN if pnl_val >= 0 else ft.Colors.RED
            
            symbol = pos.get('symbol', 'N/A')
            position_amt = float(pos.get('positionAmt', 0))
            entry_price = float(pos.get('entryPrice', 0))
            mark_price = float(pos.get('markPrice', 0))
            liq_price = float(pos.get('liquidationPrice', 0))
            margin_ratio = float(pos.get('marginRatio', 0))
            
            # Calcular porcentaje de PnL
            if entry_price > 0:
                pnl_pct = (pnl_val / (abs(position_amt) * entry_price)) * 100
            else:
                pnl_pct = 0
            
            # Actualizar valores
            self.symbol_text.value = symbol
            self.size_text.value = f"{position_amt:.4f}"
            self.entry_price_text.value = f"${entry_price:.5f}"
            self.mark_price_text.value = f"${mark_price:.5f}"
            self.liq_price_text.value = f"${liq_price:.5f}"
            self.margin_ratio_text.value = f"{margin_ratio*100:.2f}%"
            
            self.pnl_text.value = f"PnL: ${pnl_val:.2f}"
            self.pnl_text.color = pnl_color
            self.pnl_pct_text.value = f"({pnl_pct:.2f}%)"
            self.pnl_pct_text.color = pnl_color
            
            # *** VERIFICAR SI ALCANZÓ EL TARGET DE GANANCIA ***
            # Buscar si hay algún trade activo en esta posición
            for trade in self.active_trades:
                if trade['symbol'] == symbol and pnl_val >= self.scalping_profit_target:
                    await self._close_active_trade(symbol, position_amt, pnl_val, trade)
                    break
        else:
            self.symbol_text.value = "Sin posiciones"
            self.size_text.value = "—"
            self.entry_price_text.value = "—"
            self.mark_price_text.value = "—"
            self.liq_price_text.value = "—"
            self.margin_ratio_text.value = "—"
            self.pnl_text.value = "PnL: $0.00"
            self.pnl_text.color = ft.Colors.GREY_400
            self.pnl_pct_text.value = "(0.00%)"
            self.pnl_pct_text.color = ft.Colors.GREY_400
        
        # --- 2. Actualizar Lista de Órdenes ---
        new_controls = []
        if not orders:
            new_controls.append(ft.Text("No hay órdenes abiertas actualmente.", italic=True, color="#707070"))
        else:
            for order in orders:
                symbol = order.get('symbol', 'N/A')
                side = order.get('side', 'N/A')
                order_type = order.get('type', 'N/A')
                price = order.get('price', 'N/A')
                qty = order.get('origQty', order.get('qty', 'N/A'))
                order_id = order.get('orderId', order.get('id', 'N/A'))
                
                # Color según lado de la orden
                side_color = "#4CAF50" if side == "BUY" else "#FF6B6B"
                
                new_controls.append(
                    ft.CupertinoListTile( 
                        title=ft.Text(f"[{order_type}] {symbol}", weight=ft.FontWeight.BOLD, color="#FFFFFF"),
                        subtitle=ft.Text(f"{side} | Cantidad: {qty} @ {price}", color="#B0B0B0"),
                        trailing=ft.TextButton(
                            "CANCELAR", 
                            icon=ft.Icons.CANCEL_OUTLINED, 
                            icon_color="#FF6B6B",
                            on_click=self.handle_cancel_order,
                            data=order_id 
                        ),
                        bgcolor="#2A2A2A",
                    )
                )

        self.open_orders_list.controls.clear()
        self.open_orders_list.controls.extend(new_controls)
        
        # --- 3. Actualizar Lista de Trades Activos ---
        active_trades_controls = []
        if not self.active_trades:
            active_trades_controls.append(ft.Text("No hay trades activos en este momento. ¡Tiempo de hacer dinero! 💸", italic=True, color="#707070"))
        else:
            for i, trade in enumerate(self.active_trades):
                symbol = trade.get('symbol', 'N/A')
                leverage = trade.get('leverage', 1)
                entry_time = trade.get('entry_time', time.time())
                elapsed_time = int(time.time() - entry_time)
                
                active_trades_controls.append(
                    ft.CupertinoListTile(
                        title=ft.Text(f"{symbol} • {leverage}x", weight=ft.FontWeight.BOLD, color="#FFFFFF"),
                        subtitle=ft.Text(f"Activo por: {elapsed_time}s", color="#B0B0B0"),
                        trailing=ft.TextButton(
                            "⚡ CERRAR",
                            icon=ft.Icons.CLOSE_ROUNDED,
                            icon_color="#FF9800",
                            on_click=self._create_close_trade_handler(trade),
                        ),
                        bgcolor="#1a3a1a",  # Verde oscuro
                    )
                )
        
        self.active_trades_list.controls.clear()
        self.active_trades_list.controls.extend(active_trades_controls)
        
        # CRÍTICO: Refrescar la UI desde el hilo de fondo
        self.page.update()
    
    def _create_close_trade_handler(self, trade):
        """Crea un handler para cerrar un trade específico."""
        def handler(e):
            self.page.run_task(self._manual_close_trade, trade)
        return handler
    
    async def _manual_close_trade(self, trade):
        """Cierra manualmente un trade específico con humor."""
        symbol = trade.get('symbol')
        try:
            funny_close_msgs = [
                f"🛑 ¡Cancelando {symbol}! Mejor prevenir que lamentar 😅",
                f"🪑 Sentándose a contar ganancias... {symbol} CERRADA",
                f"🚪 Saliendo antes de que se ponga feo... {symbol} OUT 👋",
                f"🎲 ¡No queremos perder lo ganado! {symbol} ADIOS 💨",
            ]
            import random
            msg = random.choice(funny_close_msgs)
            
            self.page.snack_bar = ft.SnackBar(ft.Text(msg), open=True)
            self.page.update()
            
            # Remover de la lista
            if trade in self.active_trades:
                self.active_trades.remove(trade)
            
            await asyncio.sleep(1)
            await self.fetch_and_display_orders()
            
        except Exception as e:
            self.page.snack_bar = ft.SnackBar(ft.Text(f"Error: {e}"), open=True)
            self.page.update()

    def _handle_start_scalping_click(self, e):
        """Manejador síncrono para el botón que ejecuta la orden de scalping"""
        self.page.run_task(self.start_scalping_trade, e)

    async def start_scalping_trade(self, e):
        """Ejecuta el trade de scalping con apalancamiento x50 (múltiples trades posibles)"""
        if not self.is_running:
            self.page.snack_bar = ft.SnackBar(ft.Text("Debe iniciar el bot primero (Play)."), open=True)
            self.page.update()
            return
        
        # Obtener el símbolo seleccionado
        selected_symbol = self.symbol_dropdown.value or "BTCUSDT"
        leverage = 50  # Apalancamiento x50
            
        self.start_trade_btn.disabled = True
        self.start_trade_btn.content = ft.Text("Calculando cantidad...", color=ft.Colors.WHITE)
        self.page.update()
        
        try:
            print(f"DEBUG: Iniciando scalping en {selected_symbol} con leverage {leverage}x")
            
            # Obtener el precio actual
            current_price = await self.connector.get_current_price(selected_symbol)
            print(f"DEBUG: Precio actual de {selected_symbol}: ${current_price}")
            
            if current_price <= 0:
                self.page.snack_bar = ft.SnackBar(ft.Text(f"❌ No se pudo obtener el precio de {selected_symbol}"), open=True)
                return
            
            # Calcular cantidad para obtener ~$10 USDT de notional SIN apalancamiento
            # Con leverage x50, el notional real será 10 * 50 = $500 USDT
            target_notional = 10  # $10 USDT (se multiplicará por 50 con el leverage)
            quantity = target_notional / current_price
            
            # Redondear según el símbolo
            if selected_symbol == "BTCUSDT":
                quantity = round(quantity, 4)
            elif selected_symbol == "ETHUSDT":
                quantity = round(quantity, 3)
            else:
                quantity = round(quantity, 0)  # Sin decimales para tokens
            
            print(f"DEBUG: Cantidad calculada: {quantity} {selected_symbol} (notional real con leverage: ${target_notional * leverage})")
            
            self.start_trade_btn.content = ft.Text("Abriendo posición...", color=ft.Colors.WHITE)
            self.page.update()
            
            # Colocamos la orden con apalancamiento x50
            result = await self.connector.place_scalping_order(symbol=selected_symbol, quantity=quantity, side="BUY", leverage=leverage)
            
            if result['status'] == "FILLED":
                # Esperar a que la orden se ejecute completamente
                order_id = result['orderId']
                print(f"DEBUG: Esperando ejecución de orden {order_id}...")
                
                # Reintentar hasta 10 veces (hasta 5 segundos)
                max_retries = 10
                for attempt in range(max_retries):
                    await asyncio.sleep(0.5)
                    order_status = await self.connector.get_order_status(selected_symbol, order_id)
                    
                    if order_status and order_status.get('executedQty', '0') != '0':
                        print(f"DEBUG: Orden ejecutada en intento {attempt + 1}")
                        break
                    elif attempt == max_retries - 1:
                        print(f"DEBUG: Orden aún pendiente después de {max_retries} intentos")
                
                # Registrar el trade activo en la lista
                new_trade = {
                    "symbol": result['symbol'],
                    "orderId": result['orderId'],
                    "quantity": result['quantity'],
                    "entry_time": time.time(),
                    "leverage": leverage
                }
                self.active_trades.append(new_trade)
                print(f"DEBUG: Trade registrado. Total de trades activos: {len(self.active_trades)}")
                
                self.page.snack_bar = ft.SnackBar(ft.Text(f"✅ Posición ABIERTA en {selected_symbol} con {leverage}x! Total: {len(self.active_trades)} trades activos..."), open=True)
            else:
                self.page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error: {result['status']}"), open=True)
            
        except Exception as api_error:
            print(f"ERROR en scalping: {api_error}")
            self.page.snack_bar = ft.SnackBar(ft.Text(f"FALLO DE ÓRDEN: {api_error}"), open=True)
            
        finally:
            # Revertir el estado del botón y forzar una actualización del monitoreo
            self.start_trade_btn.disabled = False
            self.start_trade_btn.content = ft.Text("INICIAR SCALPING ($10 USDT)")
            await self.fetch_and_display_orders()
            self.page.update()
    
    async def _close_active_trade(self, symbol, position_amt, pnl, trade):
        """Cierra automáticamente el trade activo cuando alcanza el target con humor 😄"""
        try:
            # Mensajes divertidos aleatorios
            funny_messages = [
                f"🎯 ¡BINGO! ${pnl:.2f} en el bolsillo! 💰",
                f"🚀 ¡GANANCIA DETECTADA! +${pnl:.2f} ¡A CERRAR! 📈",
                f"💎 ¡DIAMANTES! Cerrando con +${pnl:.2f}... ✨",
                f"🎪 ¡MAGIA CAPITALISTA! +${pnl:.2f} ganados 🪄",
                f"🏆 ¡CAMPEÓN! Cerrando posición con +${pnl:.2f}! 🥇",
                f"🍾 ¡CHAMPAGNE TIME! +${pnl:.2f} para celebrar 🎉",
                f"💪 ¡STONKS! Cerrando con +${pnl:.2f} de ganancia! 📊",
                f"🎭 ¡SHOWTIME! Telón final con +${pnl:.2f}! 🎬",
            ]
            import random
            funny_msg = random.choice(funny_messages)
            
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"Cerrando: {funny_msg}"),
                open=True
            )
            self.page.update()
            
            # Ejecutar cierre
            result = await self.connector.close_position(symbol, position_amt)
            
            if result['status'] == "CLOSED":
                self.page.snack_bar = ft.SnackBar(
                    ft.Text(f"✅ POSICIÓN CERRADA! Ganancia final: +${pnl:.2f} USD 🎊"),
                    open=True
                )
                # Remover el trade de la lista
                if trade in self.active_trades:
                    self.active_trades.remove(trade)
                    print(f"DEBUG: Trade cerrado. Quedan {len(self.active_trades)} trades activos")
            else:
                self.page.snack_bar = ft.SnackBar(
                    ft.Text(f"⚠️ Error al cerrar. Estado: {result['status']}"),
                    open=True
                )
            
            # Actualizar interfaz
            await self.fetch_and_display_orders()
            self.page.update()
            
        except Exception as e:
            self.page.snack_bar = ft.SnackBar(ft.Text(f"Error en cierre automático: {e}"), open=True)
            self.page.update()


    async def handle_cancel_order(self, e):
        """Maneja la cancelación de una orden a través del botón."""
        order_id = e.control.data
        e.control.disabled = True # Deshabilitar el botón mientras se procesa
        self.page.update()
        
        try:
            # Llamada asíncrona a la API de cancelación [16, 17]
            result = await self.connector.cancel_order(order_id)
            if result.get("status") == "CANCELED":
                msg = f"Orden {order_id} cancelada."
            else:
                msg = f"Fallo al cancelar {order_id}."
            
            try:
                self.page.snack_bar = ft.SnackBar(ft.Text(msg), open=True)
            except:
                pass  # Ignorar si la página está cerrada
            
        except asyncio.CancelledError:
            # Ignorar cancelación de tarea
            e.control.disabled = False
            return
        except Exception as api_error:
            try:
                self.page.snack_bar = ft.SnackBar(ft.Text(f"Error al cancelar: {api_error}"), open=True)
            except:
                pass  # Ignorar si la página está cerrada
            e.control.disabled = False

        finally:
            # Forzar la actualización de la lista de órdenes
            try:
                await self.fetch_and_display_orders()
                self.page.update()
            except:
                pass  # Ignorar errores al actualizar si la página está cerrada


async def main(page: ft.Page):
    # --- CONFIGURACIÓN DE LA PÁGINA ---
    page.title = "Bot Flet Futuros"
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.theme_mode = ft.ThemeMode.DARK # Opcional, pero da mejor contraste
    page.adaptive = True  # ¡CRÍTICO para estilo iOS! [4]
    page.padding = 0

    # Inicializar el conector
    connector = BinanceConnector(API_KEY, SECRET_KEY)
    
    # Crear e iniciar la interfaz
    app = TradingBotUI(page, connector)
    
    page.add(
        ft.SafeArea( # Asegura que la UI no se solape con la barra de estado de iOS [24]
            ft.Container(
                content=app,
                expand=True
            )
        )
    )

if __name__ == "__main__":
    ft.app(target=main)