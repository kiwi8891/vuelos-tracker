# VUELOS TRACKER — Contexto del proyecto

## Qué hace
Monitorización diaria de precios de vuelo para un viaje familiar en octubre 2026.
Ejecuta automáticamente cada día vía **launchd en Mac Mini local** (09:00h), guarda historial en CSV,
hace git commit + push automático al repo, y manda resumen + gráfica de evolución por Telegram.
Dashboard interactivo en GitHub Pages.

## Pasajeros
- 2 adultos + 2 niños (5 años + 2-3 años, ambos con asiento propio)
- Parámetros API: adults=2, children=2, infants_on_lap=0
- **Total: 4 personas** — todos los precios se muestran en total y €/persona

## Rutas monitorizadas
Definidas en `config.json`. Actualmente dos trips:

### asia-oct-2026 (con escalas)
- **IDA**: BCN → ICN (Seúl) / TPE (Taipei) / HKG (Hong Kong) — 1 y 2 oct 2026
- **VUELTA**: PEK (Pekín) / PVG (Shanghái) / CAN (Guangzhou) → BCN — 16 y 17 oct 2026
- 4 llamadas API/día

### asia-oct-2026-directo (solo directos)
- **IDA**: BCN → ICN / TPE / HKG — 1 oct 2026 (solo 1 fecha para ahorrar cuota)
- **VUELTA**: PEK / PVG / CAN → BCN — 16 oct 2026
- max_stops: 0
- 2 llamadas API/día

**Total: 6 llamadas/día × 31 días = ~186/mes** (límite: 250 gratis)

## Stack técnico
- **API de vuelos**: SerpAPI Google Flights (free: 250 llamadas/mes)
  - Multi-aeropuerto: `arrival_id="ICN,TPE,HKG"` = 1 sola llamada
  - `price_insights.price_level` de Google para detectar precios bajos
- **Alertas**: Telegram Bot — resumen diario + gráfica PNG
- **Historial**: `data/prices.csv` — commiteado y pusheado automáticamente por `flight_tracker.py` al final de cada ejecución
- **Dashboard**: GitHub Pages → `docs/index.html` (ClickHouse design system)
- **Automatización**: launchd en Mac Mini — `~/Library/LaunchAgents/com.gerardo.vuelostracker.plist` — 09:00h hora local

## Archivos clave
- `config.json` — configuración de viajes, pasajeros y alertas (**editar aquí para cambiar búsquedas**)
- `flight_tracker.py` — script principal, lee config.json; al terminar hace git commit + push del CSV
- `.github/workflows/daily_check.yml` — workflow de GitHub Actions (ya no se usa, conservado como backup)
- `data/prices.csv` — historial de precios (columnas: date, trip_id, type, origin, destination, flight_date, price_eur, stops, airline, duration_m, price_level, typical_low, typical_high)
- `docs/index.html` — dashboard GitHub Pages
- `requirements.txt` — dependencias Python (incluye matplotlib)
- `~/Library/LaunchAgents/com.gerardo.vuelostracker.plist` — agente launchd (Mac Mini, fuera del repo)

## Cómo añadir/modificar búsquedas
Editar `config.json`. Estructura de un trip:
```json
{
  "id": "id-unico",
  "name": "Nombre visible",
  "outbound": {
    "origin": "BCN",
    "destinations": ["ICN", "TPE"],
    "dates": ["2026-10-01"],
    "max_stops": null        ← null = sin filtro, 0 = solo directos
  },
  "return": {
    "origins": ["PEK", "PVG"],
    "destination": "BCN",
    "dates": ["2026-10-16"],
    "max_stops": null
  }
}
```
Commit + push → ejecuta automáticamente en el siguiente cron (09:00h), o lanzar manualmente:
```bash
launchctl start com.gerardo.vuelostracker
```

## Benchmarks de precio
1. **Google**: `price_insights.price_level` = low / typical / high (puede estar vacío para fechas lejanas)
2. **Propio**: alerta si el precio baja ≥5% respecto a la media histórica (activa tras 3+ días de datos)

## KPIs mostrados
- Precio total del viaje (todos los pasajeros)
- **€/persona** (precio total ÷ 4) — en Telegram y dashboard

## Dashboard (GitHub Pages)
URL: https://kiwi8891.github.io/vuelos-tracker/
- Tabs por trip
- Hero con mejor combo (total + €/pers)
- Tarjetas de precio por fecha de vuelo
- Gráficas de evolución (Chart.js)
- Tabla con filtros (tipo, escalas)
- Panel de config con JSON resaltado
- Diseño: ClickHouse design system (#000 + #faff69 neon volt)

## Variables de entorno
```
SERPAPI_KEY          # serpapi.com → dashboard
TELEGRAM_BOT_TOKEN   # @BotFather en Telegram
TELEGRAM_CHAT_ID     # @userinfobot en Telegram
```
Guardadas en `.env` (local, no commiteado) y en el plist de launchd (`EnvironmentVariables`).
Los secrets de GitHub siguen configurados en el repo como backup.

## Para ejecutar localmente
```bash
pip3 install -r requirements.txt
python3 flight_tracker.py
```

## Repo
https://github.com/kiwi8891/vuelos-tracker (público)
