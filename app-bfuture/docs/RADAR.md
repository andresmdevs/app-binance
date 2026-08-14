# Radar — Motor de señales

Sistema de detección de oportunidades sobre la capa `binance/`: detecta zonas de
soporte/resistencia por volumen, mide el flujo (OI, funding, taker), puntúa
candidatos y ordena 1-3 entradas con **riesgo definido**. Es **semi-automático**:
el motor recomienda y dimensiona; **el usuario confirma cada orden** (nada se
ejecuta solo).

---

## Módulos nuevos

### `binance/signals.py` — funciones puras (sin red, testeables)

Dataclasses: `PriceBin`, `Zone`, `LiqCluster`, `Bias`, `Gates`, `Setup`.

| Función | Qué hace |
|---|---|
| `volume_profile(klines, bins)` | reparte el quoteVolume por precio → perfil |
| `high_volume_nodes(profile, top_k)` | nodos de alto volumen (imanes) |
| `sr_zones(profile, price, top_k)` | zonas soporte/resistencia por cercanía |
| `swing_levels(klines, lookback)` | máximos/mínimos locales (pivotes) |
| `cluster_liquidations(prints, bin_pct)` | agrupa liquidaciones en bandas |
| `estimated_liq_levels(price, side, leverages)` | niveles de liq. estimados |
| `oi_change_pct(hist)` / `latest_taker_ratio(rows)` | lectura de flujo |
| `directional_bias(chg, oi, funding, taker)` | sesgo `[-1,1]` → `Bias` |
| `profile_fit(aligned, entry_rsi, winner_rsi)` | parecido a tus ganadoras |
| `sma` / `rsi` / `trend_direction` / `depth_imbalance` | indicadores auxiliares |
| `net_rr(entry, stop, target, fee)` | R:R **después de fees** |
| `safe_sizing(available, sl_frac, min_notional, ...)` | margen + apalancamiento seguro |
| `passes_gates(...)` / `composite_score(...)` / `rank_setups(...)` | filtros, score, ranking |

Pesos del score (`DEFAULT_WEIGHTS`): estructura 30 · flujo 25 · combustible de
liquidación 20 · perfil ganador 15 · calidad R:R 10.

### `binance/scanner.py` — orquestación async

- `scan_symbol(client, symbol, ...) -> Setup | None`: baja klines/OI/taker/funding/
  libro, arma el setup (zona de entrada, stop estructural, objetivo, R:R neto),
  aplica los gates y devuelve un `Setup` o `None`.
- `scan_market(client, symbols, top_n, ...) -> list[Setup]`: escaneo en paralelo + ranking.
- **Dry-run** (mainnet público, sin claves): `PYTHONPATH=src .venv/bin/python -m binance.scanner [n]`
- Constantes: `STOP_BUFFER`, `MAKER_FEE_RT`, `NEAR_PCT`, `MAX_TARGET_PCT` (tope de
  objetivo, mata R:R fantasía), `ENTRY_NEAR_GATE` (proximidad: solo setups accionables ya).

### `binance/llm.py` — LLM local (Ollama)

- `narrate(prompt, model, system, ...) -> str | None`: narración con `coffex:latest`
  (keep-alive para no recargar en frío). **Anclado al motor**: usa solo los números
  del contexto, nunca inventa precios.
- `available(timeout) -> bool`.

---

## Cambios en módulos existentes

### `binance/client.py` — endpoints de datos (públicos, sin firma)
`depth`, `open_interest`, `open_interest_hist`, `long_short_account_ratio`,
`top_long_short_position_ratio`, `taker_long_short_ratio`.
Nota: los `/futures/data/*` solo existen en **mainnet**.

### `binance/scalp.py` — margen aislado
`ScalpConfig.margin_type = "ISOLATED"` por defecto; `open_scalp` llama a
`set_margin_type` antes de `set_leverage` → **la pérdida máxima queda capada al margen**.

### `src/main.py` — pestaña Radar + dimensionado con riesgo definido
- Nueva pestaña **Radar**.
- Métodos: `on_scan`, `on_analyze_symbol` (análisis on-demand de un símbolo),
  `_symbol_in_text`, `_render_setups`, `_setup_chart`, `_setup_prepare`
  (auto-dimensiona: margen ~10% del saldo, apalancamiento seguro por SL, aislado),
  `_setups_context`, `_narrate_setups`, `on_radar_chat` (chat anclado al motor).
- Constantes: `RADAR_RISK_FRAC` (0.10), `LEV_SAFETY_BUFFER` (1.6).
- Flags de entorno: `RADAR_DEMO=1` (abre en Radar y escanea al iniciar),
  `FLET_WEB_RENDERER` (renderer web opcional), `RADAR_RISK_FRAC`.

---

## Modelo de riesgo (resumen)

- **Margen por orden** ≈ 10% del disponible (~$0.69 con $6.95).
- **Apalancamiento** = el más alto que deje la **liquidación (≈1/lev en aislado)
  más lejos que el SL × 1.6**, y a la vez suficiente para el mínimo notional.
  Tope en `RISK_MAX_LEVERAGE` (10 por defecto). Sube el tope solo cuando esté probado.
- **Margen aislado** → pérdida máxima por operación = el margen, no la cuenta.
- **Tope de objetivo (6%)** + **gate de proximidad (3%)** → solo setups accionables
  ahora, con R:R realista y entrada a mercado ≈ entrada estructural.

## Flujo de uso

1. **Radar → Escanear** (top-3 accionables) o **Analizar símbolo**.
2. **Preparar orden** (auto-dimensiona; **NO ejecuta**) → te lleva al Dashboard.
3. Revisar y pulsar **LONG/SHORT → Confirmar**. Se colocan brackets TP/SL
   (condicionales) y la app gestiona break-even + trailing + cierre por tiempo.

## Tests

`tests/test_signals.py` cubre las funciones puras: volume profile, zonas, sesgo,
R:R neto, `safe_sizing`, gates y score.
