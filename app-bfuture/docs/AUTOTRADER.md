# Motor autónomo (testnet)

Automatiza **análisis + ejecución** al estilo del modo autónomo del bot de Exnova:
analiza unos segundos **antes** de que abra cada vela y ejecuta **al abrirla**. Usa
dos fuentes de señales sobre el mismo ejecutor:

- **Vela (votos)**: cada indicador vota sobre las velas de 1m (u otro intervalo).
- **Radar**: las señales del motor de señales existente (`scanner.scan_market`),
  ejecutadas sin pasar por "Preparar orden → Confirmar".

> **Solo TESTNET.** El motor se niega a arrancar contra producción. Primero hay
> que medir; los precios de testnet son sintéticos, así que la rentabilidad
> real se mide con el **replay** sobre histórico real de mainnet (ver abajo).

---

## Ciclo por vela

```
T−4s    ANALIZA   últimas 99 velas → votos → gates → riesgo → margen aislado +
                  apalancamiento ya fijados (nada de esto ocurre en el instante crítico)
T+0.3s  EJECUTA   MARKET + bracket TP/SL (órdenes condicionales en el exchange:
                  protegen aunque la app se cierre)
…       SUPERVISA break-even/trailing opcional; cierre por tiempo ~6 s antes del
                  siguiente análisis → puede operar una vez por vela, como Exnova
fin     MIDE      PnL NETO real (userTrades: realizado − comisiones), racha, diario
```

Si el análisis se retrasa más de 2 s pasada la apertura, **salta la vela** (no
entra tarde).

## Cómo decide (modo vela)

| Voto | Qué mide | +1 (LONG) | −1 (SHORT) |
|---|---|---|---|
| Tendencia | EMA 9 vs EMA 21 | rápida arriba | rápida abajo |
| MACD | histograma 12/26/9 | impulso alcista | impulso bajista |
| RSI 14 | sobrecompra/sobreventa | < 30 (espera rebote) | > 70 |
| Bollinger 20·2σ | precio fuera de banda | bajo la inferior | sobre la superior |
| Flujo taker | % de volumen de compra agresiva (5 velas) | > 55% | < 45% |

"Flujo taker" sustituye al resumen técnico de Exnova (que nunca llegó para OTC) y
viene de las propias velas (la misma idea que el `taker ratio` del Radar).

**Diferencias deliberadas con Exnova** (aquí no son binarias):

| Exnova | Este motor | Por qué |
|---|---|---|
| Empate → color de la última vela | Empate o suma débil → **no opera** | En futuros saltar una vela es gratis; ese desempate acertó 33% |
| Una orden por vela, siempre | Solo si `|suma| ≥ votos mín.` (2 por defecto) | Menos órdenes, más convicción |
| Pago fijo | **Gate de volatilidad**: TP (1.5×ATR) ≥ 2× comisión ida+vuelta | En 1m de pares tranquilos el movimiento no paga las comisiones |
| Martingala | **Tamaño fijo**; pausa tras N pérdidas seguidas | La martingala apalancada termina en liquidación |

TP y SL salen del ATR(14) de la propia vela: `TP = 1.5×ATR`, `SL = 1.0×ATR`.
El apalancamiento se recorta para que la liquidación (~1/lev en aislado) quede más
lejos que el SL × 1.6.

## Modo radar

Cada 5 min (25 s antes de la vela) corre el escáner sobre los símbolos indicados
(o el top 20 más líquido), toma los setups con score ≥ 55 y los ejecuta con sus %
de stop/objetivo aplicados al precio de la cuenta (igual que "Preparar orden").
Duración máx 60 min con trailing, y enfriamiento de 3 velas por símbolo.

## Seguridad

- Solo testnet (producción bloqueada en código).
- `RiskManager.check_open` antes de **cada** orden. El **kill-switch diario detiene
  el motor** (`KillSwitchError`); otros bloqueos solo saltan la señal.
- Pausa tras **N pérdidas seguidas** (4 por defecto), tope de órdenes por sesión,
  máx. 2 posiciones del motor a la vez, una por símbolo.
- **Nunca** toca un símbolo con una posición que no abrió él.
- 3 entradas fallidas seguidas o 5 análisis fallidos seguidos → se detiene.
- "Detener y cerrar" (o cerrar la app) cierra a mercado lo que tenga abierto.

## Uso

### En la app: pestaña **Auto**

Elige modo, símbolos, margen y apalancamiento máx. → **Arrancar**. La actividad
muestra cada vela (por qué se operó o se descartó), cada entrada y cierre con su PnL
neto, y abajo el **marcador en sombra**.

En Render (free) la app se duerme sin tráfico: el motor solo vive mientras la app
esté abierta. Para dejarlo corriendo horas, usa el CLI en tu Mac.

### CLI

```bash
cd app-bfuture

# 1) PRIMERO: medir las reglas en histórico REAL (mainnet, solo lectura, sin claves)
PYTHONPATH=src uv run python -m binance.autotrader replay --symbols BTCUSDT ETHUSDT SOLUSDT --days 7

# 2) Operar en TESTNET (Ctrl+C detiene y cierra sus posiciones)
PYTHONPATH=src uv run python -m binance.autotrader run --symbols SOLUSDT DOGEUSDT --minutes 60
PYTHONPATH=src uv run python -m binance.autotrader run --mode radar --minutes 120 --quiet
```

Parámetros útiles (ambos comandos): `--interval`, `--min-score`, `--votes trend macd flow`
(qué votos cuentan), `--tp-atr`, `--sl-atr`, `--hold` (velas), `--fee`, `--min-edge`,
`--margin`, `--leverage`. Solo `run`: `--lead` (s antes de la vela), `--max-trades`,
`--max-losses`, `--concurrent`, `--trailing`, `--maker`.

## Diario y marcador en sombra

- `logs/autotrader.jsonl`: cada vela analizada (votos **y valores** de cada
  indicador, decisión y motivo), cada entrada y cada cierre con PnL neto.
- `logs/autotrader-orders.jsonl` (CLI) / `logs/orders.jsonl` (app): auditoría de
  órdenes de siempre.
- **Marcador en sombra**: en **todas** las velas (operadas o no) anota qué votó cada
  indicador y lo compara con la dirección real de la vela. Se ordena por el extremo
  inferior del intervalo de confianza al 95%: con pocas velas el intervalo es ancho
  y no se puede afirmar nada.

## Lectura honesta

- En 1m, la comisión ida+vuelta (≈0.10% taker) es del tamaño del movimiento típico
  de BTC/ETH. Por eso el gate de volatilidad descarta la mayoría de velas en pares
  tranquilos: es intencionado.
- El replay decide con el cierre de la vela; en vivo se decide 4 s antes. Es una
  aproximación optimista de pocos segundos.
- Testnet valida la **mecánica** (tiempos, órdenes, brackets, cierres, PnL), no la
  rentabilidad. Antes de pensar en real: replay positivo en varias semanas y
  símbolos, con ≥ 300 órdenes y fuera de muestra.

## Módulos

| Archivo | Qué hace |
|---|---|
| `binance/autosignal.py` | Puro: indicadores, `decide`, `ShadowBoard`, `simulate` (replay), `summarize_sim` |
| `binance/engine.py` | `AutoEngine`: scheduler alineado a la vela, modos vela/radar, riesgo, ejecución, supervisión, PnL |
| `binance/autotrader.py` | CLI `run` / `replay` |
| `binance/scalp.py` | `ScalpConfig.preconfigured`; el cierre por tiempo ya no se pasa del deadline |
| `binance/risk.py` | `KillSwitchError`, `limits_from_env()` |
| `src/main.py` | Pestaña **Auto** |

Tests: `tests/test_autosignal.py`, `tests/test_engine.py` (reloj y exchange falsos:
una sesión de varios minutos corre en milisegundos), `tests/test_autotrader.py`.
