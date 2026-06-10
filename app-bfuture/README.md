# AppBfuture app

Dashboard de Binance Futures hecho con Flet.

## Configuración (credenciales de Binance)

La app necesita una API key de Binance **Futures** con permisos de *Reading + Futures*
(no requiere retiros). Las credenciales se proveen en este orden de prioridad:

1. **Variables de entorno del sistema** (recomendado para producción y builds):
   `BINANCE_API_KEY` y `BINANCE_API_SECRET`.
2. **Archivo `.env` en la raíz del proyecto** (`app-bfuture/.env`):

   ```bash
   cp .env.example .env    # luego edita .env con tus valores reales
   ```

   ```env
   BINANCE_API_KEY=tu_api_key
   BINANCE_API_SECRET=tu_api_secret
   ```

> ⚠️ **No coloques el `.env` dentro de `src/assets/`.** Esa carpeta se empaqueta dentro
> del ejecutable al hacer `flet build`, lo que filtraría tus claves en cualquier build
> distribuible. El `.env` vive en la raíz del proyecto (fuera del bundle `src/`) y está
> en `.gitignore`.

## Despliegue en Render (acceso desde iPhone)

La app puede servirse como web app en [Render](https://render.com) usando el
blueprint `render.yaml` de la raíz del repo. Free tier: el servicio **duerme** a
los ~15 min sin tráfico (~1 min en despertar); dormido NO corre trailing/cierre
por tiempo (los TP/SL sí persisten en el exchange).

1. Sube el repo a GitHub (**privado**).
2. En Render: **New → Blueprint** → selecciona el repo (detecta `render.yaml`,
   región **Frankfurt** — obligatorio: Binance bloquea IPs de EE.UU.).
3. Rellena las env vars cuando lo pida: `BINANCE_API_KEY`/`BINANCE_API_SECRET`
   (testnet primero) y `APP_ACCESS_KEY` (clave de login de la app — elige una
   larga; sin ella cualquiera con la URL podría operar tu cuenta).
4. Deploy. Abre la URL en Safari del iPhone → Compartir → **Añadir a pantalla
   de inicio** → se usa como app.

Notas: el `.env` local NO se sube (gitignored); en Render todo va por env vars.
El log de auditoría en Render es efímero (disco se borra al dormir/redeploy);
el historial real vive en Binance. Para producción real, considera IP dedicada
(de pago) + regenerar claves con permisos mínimos.

## Run the app

### uv

Run as a desktop app:

```
uv run flet run
```

Run as a web app:

```
uv run flet run --web
```

### Poetry

Install dependencies from `pyproject.toml`:

```
poetry install
```

Run as a desktop app:

```
poetry run flet run
```

Run as a web app:

```
poetry run flet run --web
```

For more details on running the app, refer to the [Getting Started Guide](https://flet.dev/docs/getting-started/).

## Build the app

### Android

```
flet build apk -v
```

For more details on building and signing `.apk` or `.aab`, refer to the [Android Packaging Guide](https://flet.dev/docs/publish/android/).

### iOS

```
flet build ipa -v
```

For more details on building and signing `.ipa`, refer to the [iOS Packaging Guide](https://flet.dev/docs/publish/ios/).

### macOS

```
flet build macos -v
```

For more details on building macOS package, refer to the [macOS Packaging Guide](https://flet.dev/docs/publish/macos/).

### Linux

```
flet build linux -v
```

For more details on building Linux package, refer to the [Linux Packaging Guide](https://flet.dev/docs/publish/linux/).

### Windows

```
flet build windows -v
```

For more details on building Windows package, refer to the [Windows Packaging Guide](https://flet.dev/docs/publish/windows/).