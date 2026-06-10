# Backtester — futuros USDⓈ-M (BTC/ETH)

Herramienta de **investigación** para probar estrategias sobre datos históricos
reales de Binance Futures, con cero riesgo. Vive fuera de `src/`, así que **no** se
empaqueta en el app móvil ni añade pandas/numpy al build.

## Por qué existe

No se "replica" una estrategia rentable copiando trades de otra persona: se
**construye** una regla explícita y se comprueba sobre años de datos si tiene una
ventaja real, *después* de descontar comisiones y slippage. Eso es lo que hace esto.

## Uso

```bash
# Comparación por defecto: BTC y ETH, 1h, tendencia EMA, 540 días
uv run --group backtest python -m backtest

# Otra estrategia / intervalo / ventana
uv run --group backtest python -m backtest --strategy rsi_rev --interval 15m --days 365
uv run --group backtest python -m backtest --start 2023-01-01 --end 2024-01-01 --interval 4h

# Solo largos, sin take-profit fijo, con gráfico de equity
uv run --group backtest python -m backtest --no-short --no-tp --plot
```

### Parámetros principales

| Flag | Por defecto | Qué hace |
|------|-------------|----------|
| `--symbols` | `BTCUSDT ETHUSDT` | pares a probar |
| `--interval` | `1h` | temporalidad de las velas |
| `--strategy` | `ema_atr` | `ema_atr`, `rsi_rev` o `donchian` |
| `--days` / `--start`/`--end` | `540` | ventana histórica |
| `--capital` | `1000` | capital inicial (USDT) |
| `--risk` | `0.01` | fracción del equity arriesgada por trade (1 %) |
| `--leverage` | `5` | tope de apalancamiento |
| `--fee` | `0.0004` | comisión taker (0.04 %) |
| `--slippage-bps` | `1.0` | slippage por lado |
| `--sl-atr` / `--tp-atr` | `2.0` / `3.0` | stop y objetivo en múltiplos de ATR |

## Cómo lee los resultados sin engañarte

- **Profit factor** < 1 → pierde dinero. Por encima de ~1.3 empieza a ser interesante.
- **Max drawdown**: la peor caída pico-a-valle. Si no podrías aguantarlo emocional o
  financieramente, la estrategia no te sirve aunque sea rentable.
- **Nº de trades**: con menos de ~30-50 operaciones, los resultados son ruido, no señal.
- **Buy & hold**: si la estrategia no le gana a "comprar y esperar", no compensa el
  riesgo ni el trabajo.

## Supuestos y limitaciones (importante)

El motor está hecho para **no** dar resultados demasiado bonitos:

- Ejecuta en la **apertura de la vela siguiente** a la señal → sin lookahead.
- SL/TP intra-vela; si ambos se tocan en la misma vela asume el **peor caso** (stop).
- Aplica **comisión taker + slippage** en cada entrada y salida.
- Sizing por **riesgo** (% del equity hasta el stop), con tope de apalancamiento.

Lo que **todavía NO** modela (tenerlo presente):

- **Funding de perpetuos** (cada 8h). Afecta sobre todo a estrategias que mantienen
  posición muchas horas. Es la mejora #1 pendiente.
- **Liquidación** por margen de mantenimiento (los stops se mantienen muy por dentro
  vía el tope de apalancamiento, pero no se simula la mecánica exacta).
- Profundidad real del libro / ejecución parcial.

## Trampa a evitar: sobreajuste (overfitting)

Es fácil encontrar parámetros que *en el pasado* dan números espectaculares y *en el
futuro* fallan. Antes de creerte una estrategia:

1. Pruébala en BTC **y** ETH (si solo funciona en uno, sospecha).
2. Reserva un tramo de datos que no usaste para afinar y pruébala ahí (out-of-sample).
3. Desconfía de cualquier combinación que necesite parámetros muy específicos para brillar.
