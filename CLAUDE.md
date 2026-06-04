# VUELOS TRACKER — Contexto del proyecto

## Qué hace
Monitorización diaria de precios de vuelo para viajes familiares.
Ejecuta automáticamente cada día vía **launchd en Mac Mini local** (09:00h), guarda historial en CSV,
hace git commit + push automático al repo, y manda resumen + gráfica de evolución por Telegram.

## Pasajeros
Configurados en `config.json`:
- `adults`, `children`, `infants_on_lap`
- Todos los precios se muestran en total y €/persona (total ÷ nº adultos + niños)

## Rutas monitorizadas
Definidas en `config.json` → array `trips`. Ver sección "Cómo añadir/modificar búsquedas".

## Stack técnico
- **API de vuelos**: SerpAPI Google Flights (free: 250 llamadas/mes)
  - Multi-aeropuerto: `arrival_id="ICN,TPE,HKG"` = 1 sola llamada
  - `price_insights.price_level` de Google para detectar precios bajos
- **Alertas**: Telegram Bot — resumen diario + gráfica PNG
- **Historial**: `data/prices.csv` — commiteado y pusheado automáticamente por `flight_tracker.py`
- **Automatización**: launchd en Mac Mini — `~/Library/LaunchAgents/com.gerardo.vuelostracker.plist`

## Archivos clave
- `config.json` — configuración de viajes, pasajeros y alertas (**editar aquí para cambiar búsquedas**)
- `flight_tracker.py` — script principal, lee config.json; al terminar hace git commit + push del CSV
- `.github/workflows/daily_check.yml` — workflow de GitHub Actions (backup, no activo)
- `data/prices.csv` — historial de precios (columnas: date, trip_id, type, origin, destination, flight_date, price_eur, stops, airline, duration_m, price_level, typical_low, typical_high)
- `requirements.txt` — dependencias Python (incluye matplotlib)
- `~/Library/LaunchAgents/com.gerardo.vuelostracker.plist` — agente launchd (Mac Mini, fuera del repo)

## Cómo añadir/modificar búsquedas
Editar `config.json`. Estructura de un trip y de un combo cross-trip:
```json
{
  "id": "id-unico",
  "name": "Nombre visible",
  "outbound": {
    "origin": "BCN",
    "destinations": ["ICN", "TPE"],
    "dates": ["2026-10-01"],
    "max_stops": null
  },
  "return": {
    "origins": ["PEK", "PVG"],
    "destination": "BCN",
    "dates": ["2026-10-16"],
    "max_stops": null
  }
}
```
- `outbound` es opcional (omitir para trips solo-vuelta)
- `max_stops`: `null` = sin filtro, `0` = solo directos

Estructura de un combo (sin llamadas API extra, cruza datos de trips existentes):
```json
{
  "name": "Nombre visible",
  "outbound_trip": "id-del-trip-que-tiene-la-ida",
  "return_trip":   "id-del-trip-que-tiene-la-vuelta"
}
```

Commit + push → ejecuta en el siguiente cron (09:00h), o lanzar manualmente:
```bash
launchctl start com.gerardo.vuelostracker
```

## Stop-loss
Configurado en `config.json` → array `stop_loss`. Alerta si el precio sube por encima de un umbral:
```json
{
  "label": "Descripción",
  "trip_id": "id-del-trip",
  "type": "outbound",
  "origin": "BCN",
  "destination": "ICN",
  "anchor_price": 1428,
  "threshold_pct": 0.03
}
```

## Benchmarks de precio
1. **Google**: `price_insights.price_level` = low / typical / high
2. **Propio**: alerta si el precio baja ≥X% respecto a la media histórica (activa tras 3+ días)

## Variables de entorno
```
SERPAPI_KEY          # serpapi.com
TELEGRAM_BOT_TOKEN   # @BotFather en Telegram
TELEGRAM_CHAT_ID     # @userinfobot en Telegram
```
Guardadas en `.env` (local, no commiteado) y en el plist de launchd (`EnvironmentVariables`).

## Para activar/desactivar launchd
```bash
launchctl load   ~/Library/LaunchAgents/com.gerardo.vuelostracker.plist
launchctl unload ~/Library/LaunchAgents/com.gerardo.vuelostracker.plist
```

## Para ejecutar localmente
```bash
pip3 install -r requirements.txt
python3 flight_tracker.py
```

## Repo
https://github.com/kiwi8891/vuelos-tracker (público)
