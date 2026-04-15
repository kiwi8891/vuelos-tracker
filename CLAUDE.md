# VUELOS TRACKER — Contexto del proyecto

## Qué hace
Monitorización diaria de precios de vuelo para un viaje familiar en octubre 2026.
Ejecuta automáticamente cada día vía GitHub Actions, guarda historial en CSV y
manda resumen + alertas por Telegram.

## Pasajeros
- 2 adultos
- 2 niños (5 años + 2-3 años, ambos con asiento propio)
- Parámetros API: adults=2, children=2, infants_on_lap=0

## Rutas monitorizadas
- **IDA**: BCN → ICN (Seúl) / TPE (Taipei) / HKG (Hong Kong)
  - Fechas: 1 y 2 de octubre 2026
- **VUELTA**: PEK (Pekín) / PVG (Shanghái) / CAN (Guangzhou) → BCN
  - Fechas: 16 y 17 de octubre 2026
- Preferencia: menos escalas mejor (directo > 1 escala > 2 escalas)

## Stack técnico
- **API de vuelos**: SerpAPI Google Flights (free: 250 llamadas/mes)
  - 4 llamadas/día × 31 días = ~124/mes (holgura del 50%)
  - Usa `price_insights.price_level` de Google para detectar precios bajos
- **Alertas**: Telegram Bot
- **Historial**: `data/prices.csv` — commiteado automáticamente por GitHub Actions
- **Automatización**: GitHub Actions cron `0 7 * * *` (09:00 CET)

## Archivos clave
- `flight_tracker.py` — script principal
- `.github/workflows/daily_check.yml` — cron de GitHub Actions
- `data/prices.csv` — historial de precios (generado automáticamente)
- `requirements.txt` — dependencias Python

## Variables de entorno / Secrets de GitHub
```
SERPAPI_KEY          # serpapi.com → dashboard
TELEGRAM_BOT_TOKEN   # @BotFather en Telegram
TELEGRAM_CHAT_ID     # @userinfobot en Telegram
```

## Lógica de alertas
- Google Flights devuelve `price_insights.price_level` = "low" / "typical" / "high"
- Si cualquier vuelo es "low" → mensaje Telegram con 🚨 ALERTA
- Telegram recibe resumen DIARIO siempre (con o sin alerta)

## Para ejecutar localmente
```bash
cp .env.example .env   # rellenar con keys reales
pip install -r requirements.txt
python flight_tracker.py
```

## Para ejecutar manualmente en GitHub
Repo → Actions → "Daily Flight Price Check" → Run workflow
