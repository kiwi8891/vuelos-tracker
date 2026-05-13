#!/usr/bin/env python3
"""
Vuelos Tracker — multi-trip, config-driven
Monitorización diaria de precios. Configurar en config.json.

Benchmarks:
  1. Google price_level (low/typical/high)
  2. Media propia histórica (activa tras OWN_MIN_SAMPLES días)
"""

import os
import csv
import json
import statistics
import io
import subprocess
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from serpapi import GoogleSearch

load_dotenv()

SERPAPI_KEY        = os.environ["SERPAPI_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

CONFIG_FILE = Path("config.json")
DATA_FILE   = Path("data/prices.csv")

FIELDNAMES = ["date", "trip_id", "type", "origin", "destination", "flight_date",
              "price_eur", "stops", "airline", "duration_m",
              "price_level", "typical_low", "typical_high"]

IATA = {
    "ICN": "Seúl",      "TPE": "Taipei",     "HKG": "Hong Kong",
    "PEK": "Pekín",     "PVG": "Shanghái",   "CAN": "Guangzhou",
    "BCN": "Barcelona", "MAD": "Madrid",      "LHR": "Londres",
    "NRT": "Tokio",     "KIX": "Osaka",       "BKK": "Bangkok",
    "SIN": "Singapur",  "DXB": "Dubai",       "DOH": "Doha",
    "SZX": "Shenzhen",
}

SWEEP_FILE = Path("data/date_sweeps.csv")
SWEEP_FIELDS = ["date", "trip_id", "type", "origin", "destination",
                "config_date", "best_date", "best_price_total", "delta_vs_config_pct"]


# ── flight-goat integration (gratis, sin API key) ─────────────────────────────

FLIGHT_GOAT_BIN = shutil.which("flight-goat-pp-cli") or str(Path.home() / "go/bin/flight-goat-pp-cli")

# Activado al detectar quota agotada en SerpAPI: el resto del run usa flight-goat
SERPAPI_EXHAUSTED = False
_QUOTA_PATTERNS = ("run out", "ran out", "exceeded", "quota", "limit reached",
                   "monthly searches", "hourly searches", "no searches left")


def _is_quota_error(msg):
    if not msg:
        return False
    m = str(msg).lower()
    return any(p in m for p in _QUOTA_PATTERNS)


def _run_flight_goat(args, timeout=30):
    """Ejecuta flight-goat-pp-cli con --agent y devuelve dict/list (o None si falla)."""
    if not Path(FLIGHT_GOAT_BIN).exists():
        return None
    try:
        r = subprocess.run(
            [FLIGHT_GOAT_BIN, *args, "--agent"],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def gf_dates_sweep(origin, destinations, target_dates, pax, padding=5, max_stops=None):
    """Busca el día más barato en ventana ±padding alrededor de target_dates.
    `destinations` puede ser lista (multi-aeropuerto) — itera sobre cada uno.
    Devuelve el mejor día y su precio total (×pax)."""
    if not target_dates:
        return None
    target_objs = [datetime.strptime(d, "%Y-%m-%d").date() for d in target_dates]
    d_from = (min(target_objs) - timedelta(days=padding)).strftime("%Y-%m-%d")
    d_to   = (max(target_objs) + timedelta(days=padding)).strftime("%Y-%m-%d")

    best = None
    for dest in destinations:
        args = ["dates", origin, dest, "--from", d_from, "--to", d_to,
                "--currency", "EUR", "--sort", "--limit", "5"]
        if max_stops == 0:
            args += ["--stops", "non_stop"]
        data = _run_flight_goat(args, timeout=20)
        if not data:
            continue
        for entry in (data.get("dates") or []):
            total = entry.get("price", 0) * pax
            if best is None or total < best["price_total"]:
                best = {
                    "origin": origin, "destination": dest,
                    "date": entry["departure_date"],
                    "price_total": total,
                }
    return best


def gf_validate(origin, destination, fly_date, pax, max_stops=None):
    """Cross-check con Google Flights directo. Devuelve precio total más barato encontrado."""
    args = ["flights", origin, destination, fly_date,
            "--currency", "EUR", "--passengers", str(pax),
            "--sort", "cheapest"]
    if max_stops == 0:
        args += ["--stops", "non_stop"]
    data = _run_flight_goat(args, timeout=25)
    if not data:
        return None
    flights = (data.get("flights") or data.get("results") or [])
    if not flights:
        return None
    prices = [f.get("price") for f in flights if isinstance(f.get("price"), (int, float))]
    return min(prices) if prices else None


def predict_trend(history_prices, current_price):
    """Predicción simple basada en pendiente últimos 7 vs 14 días.
    Devuelve (emoji, etiqueta, razonamiento_corto) o None si no hay datos."""
    if len(history_prices) < 5:
        return None
    recent = history_prices[-7:]
    older  = history_prices[-14:-7] if len(history_prices) >= 14 else history_prices[:-7]
    if not older:
        return None
    avg_recent = statistics.mean(recent)
    avg_older  = statistics.mean(older)
    diff_pct = (avg_recent - avg_older) / avg_older if avg_older else 0

    if diff_pct > 0.03:
        emoji, label = "⬆️", "SUBIENDO"
        reason = f"{diff_pct*100:+.1f}% vs sem.anterior"
    elif diff_pct < -0.03:
        emoji, label = "⬇️", "BAJANDO"
        reason = f"{diff_pct*100:+.1f}% vs sem.anterior"
    else:
        emoji, label = "➡️", "ESTABLE"
        reason = f"±{abs(diff_pct)*100:.1f}%"

    # Posición del precio actual respecto al rango histórico
    lo, hi = min(history_prices), max(history_prices)
    if hi > lo:
        pos = (current_price - lo) / (hi - lo)
        if pos < 0.20:
            reason += " · cerca mínimo"
        elif pos > 0.80:
            reason += " · cerca máximo"
    return emoji, label, reason


def save_sweep(row):
    SWEEP_FILE.parent.mkdir(exist_ok=True)
    write_header = not SWEEP_FILE.exists()
    with open(SWEEP_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SWEEP_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(row)


# ── Stop-loss alerts (basados en ancla + threshold definido en config.json) ──

def check_stop_loss(stop_losses, trip_id, type_, origin, destination, current_price):
    """Busca regla de stop-loss aplicable y devuelve estado.
    Match permisivo: si origin no se especifica, cualquier origin coincide
    (útil para reglas que aplican a 'cualquier vuelta dentro de un trip')."""
    if not stop_losses:
        return None
    for sl in stop_losses:
        if sl.get("trip_id") != trip_id or sl.get("type") != type_:
            continue
        if sl.get("origin") and sl["origin"] != origin:
            continue
        if sl.get("destination") and sl["destination"] != destination:
            continue
        anchor    = float(sl["anchor_price"])
        threshold = float(sl.get("threshold_pct", 0.05))
        trigger   = anchor * (1 + threshold)
        diff_pct  = (current_price - anchor) / anchor if anchor else 0
        triggered = current_price >= trigger
        return {
            "label":    sl.get("label", f"{type_} {origin}→{destination}"),
            "anchor":   anchor,
            "trigger":  trigger,
            "current":  current_price,
            "diff_pct": diff_pct,
            "triggered": triggered,
        }
    return None


def fmt_stop_loss(sl):
    if not sl:
        return None
    arrow = "▲" if sl["diff_pct"] > 0 else ("▼" if sl["diff_pct"] < 0 else "·")
    if sl["triggered"]:
        return (f"🚨 <b>STOP-LOSS DISPARADO</b> · ancla €{int(sl['anchor'])} → "
                f"trigger €{round(sl['trigger'])} · actual €{int(sl['current'])} "
                f"({arrow}{abs(sl['diff_pct'])*100:.1f}%)")
    return (f"🛑 stop-loss €{round(sl['trigger'])} ({arrow}{abs(sl['diff_pct'])*100:.1f}% "
            f"vs ancla €{int(sl['anchor'])})")


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── Histórico ─────────────────────────────────────────────────────────────────

def load_history():
    """Agrupa por (trip_id, origin, destination, flight_date) para no mezclar
    precios de configuraciones distintas (directo vs con escalas)."""
    if not DATA_FILE.exists():
        return {}
    history = {}
    with open(DATA_FILE, newline="") as f:
        for row in csv.DictReader(f):
            tid = row.get("trip_id") or "default"
            key = f"{tid}|{row['origin']}-{row['destination']}-{row['flight_date']}"
            history.setdefault(key, []).append(float(row["price_eur"]))
    return history


def hkey(trip_id, origin, destination, flight_date):
    return f"{trip_id}|{origin}-{destination}-{flight_date}"


def own_benchmark(history, key, current_price, cfg_alerts):
    prices      = history.get(key, [])
    min_samples = cfg_alerts.get("own_min_samples", 3)
    alert_pct   = cfg_alerts.get("own_alert_pct", 0.05)
    if len(prices) < min_samples:
        return None
    avg      = statistics.mean(prices)
    diff_pct = (current_price - avg) / avg
    is_alert = diff_pct <= -alert_pct
    return avg, diff_pct, is_alert


# ── API ───────────────────────────────────────────────────────────────────────

def _search_flights_via_goat(departure_id, arrival_id, outbound_date, passengers):
    """Fallback flight-goat. Devuelve (flights, insights) compatible con parse_best.
    `departure_id` y `arrival_id` pueden ser CSV (multi-aeropuerto); itera combos.
    flight-goat con --passengers N devuelve precio TOTAL para N pax (igual SerpAPI)."""
    pax = passengers["adults"] + passengers.get("children", 0)
    if pax < 1:
        pax = 1
    origins  = [o.strip() for o in departure_id.split(",") if o.strip()]
    arrivals = [a.strip() for a in arrival_id.split(",")   if a.strip()]
    out = []
    for orig in origins:
        for arr in arrivals:
            args = ["flights", orig, arr, outbound_date,
                    "--currency", "EUR", "--passengers", str(pax),
                    "--sort", "cheapest"]
            data = _run_flight_goat(args, timeout=25)
            if not data:
                continue
            for fg in (data.get("flights") or []):
                legs  = fg.get("legs") or []
                price = fg.get("price", 0)
                if not legs or not isinstance(price, (int, float)) or price <= 0:
                    continue  # flight-goat marca price=0 si la aerolínea no expone precio
                synth = [{
                    "departure_airport": {"id": (l.get("departure_airport") or {}).get("code", "?")},
                    "arrival_airport":   {"id": (l.get("arrival_airport")   or {}).get("code", "?")},
                    "airline":            (l.get("airline") or {}).get("name", "?"),
                } for l in legs]
                out.append({
                    "flights":        synth,
                    "price":          fg.get("price", 0),
                    "total_duration": fg.get("duration", 0),
                })
    return out, {}  # insights vacío: sin price_level ni typical_price_range


def search_flights(departure_id, arrival_id, outbound_date, label, passengers):
    global SERPAPI_EXHAUSTED
    print(f"  Buscando {label}...", end=" ", flush=True)

    if SERPAPI_EXHAUSTED:
        flights, insights = _search_flights_via_goat(departure_id, arrival_id, outbound_date, passengers)
        print(f"OK fallback ({len(flights)} vuelos)")
        return flights, insights

    params = {
        "engine":         "google_flights",
        "departure_id":   departure_id,
        "arrival_id":     arrival_id,
        "outbound_date":  outbound_date,
        "type":           "2",
        "adults":         passengers["adults"],
        "children":       passengers.get("children", 0),
        "infants_on_lap": passengers.get("infants_on_lap", 0),
        "currency":       "EUR",
        "hl":             "es",
        "api_key":        SERPAPI_KEY,
    }
    try:
        results = GoogleSearch(params).get_dict()
        err     = results.get("error")
        if err and _is_quota_error(err):
            SERPAPI_EXHAUSTED = True
            print(f"QUOTA AGOTADA — switch a flight-goat")
            flights, insights = _search_flights_via_goat(departure_id, arrival_id, outbound_date, passengers)
            print(f"    fallback OK ({len(flights)} vuelos)")
            return flights, insights
        if err:
            print(f"ERROR: {err}")
            return [], {}
        flights  = results.get("best_flights", []) + results.get("other_flights", [])
        insights = results.get("price_insights", {})
        print(f"OK ({len(flights)} vuelos)")
        return flights, insights
    except Exception as e:
        if _is_quota_error(e):
            SERPAPI_EXHAUSTED = True
            print(f"QUOTA AGOTADA — switch a flight-goat")
            flights, insights = _search_flights_via_goat(departure_id, arrival_id, outbound_date, passengers)
            print(f"    fallback OK ({len(flights)} vuelos)")
            return flights, insights
        print(f"ERROR: {e}")
        return [], {}


def parse_best(flights, max_stops=None):
    if not flights:
        return None
    candidates = flights
    if max_stops is not None:
        candidates = [f for f in flights if len(f.get("flights", [])) - 1 <= max_stops]
    if not candidates:
        return None
    best = min(candidates, key=lambda f: f.get("price", float("inf")))
    legs = best.get("flights", [])
    return {
        "origin":      legs[0]["departure_airport"]["id"]  if legs else "?",
        "destination": legs[-1]["arrival_airport"]["id"]   if legs else "?",
        "price":       best.get("price", 0),
        "stops":       len(legs) - 1,
        "airline":     legs[0].get("airline", "?")         if legs else "?",
        "duration_m":  best.get("total_duration", 0),
    }


# ── Formato ───────────────────────────────────────────────────────────────────

def google_label(insights):
    level = insights.get("price_level", "").lower()
    return {"low": ("🟢", "BAJO"), "typical": ("🟡", "TÍPICO"), "high": ("🔴", "ALTO")}.get(level, ("⚪", "—"))


def fmt_duration(m):
    h, mm = divmod(m, 60)
    return f"{h}h{mm:02d}m"


def fmt_date(d):
    parsed = datetime.strptime(d, "%Y-%m-%d")
    return f"{parsed.day} {parsed.strftime('%b').lower()}"


def fmt_price(total, pax):
    t = int(total)
    return f"€{t} (€{t // pax}/pers)"


def fmt_own(result):
    if result is None:
        return "📊 acumulando…"
    avg, diff_pct, is_alert = result
    sign  = "▼" if diff_pct < 0 else "▲"
    badge = " 🚨 <b>MÍNIMO</b>" if is_alert else ""
    return f"📊 €{avg:.0f} ({sign}{abs(diff_pct)*100:.1f}%){badge}"


# ── Persistencia ──────────────────────────────────────────────────────────────

def save_record(row):
    DATA_FILE.parent.mkdir(exist_ok=True)
    write_header = not DATA_FILE.exists()
    with open(DATA_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            w.writeheader()
        w.writerow(row)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  (Telegram no configurado — saltando)")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=10,
    )
    if not r.ok:
        print(f"  Telegram error: {r.text}")


def send_telegram_photo(image_bytes):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
        data={"chat_id": TELEGRAM_CHAT_ID},
        files={"photo": ("chart.png", image_bytes, "image/png")},
        timeout=15,
    )
    if not r.ok:
        print(f"  Telegram photo error: {r.text}")


# ── Gráfica ───────────────────────────────────────────────────────────────────

def generate_chart():
    """Genera gráfica de evolución de precios y la envía a Telegram."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from datetime import datetime as dt
    except ImportError:
        print("  (matplotlib no instalado — saltando gráfica)")
        return

    if not DATA_FILE.exists():
        return

    with open(DATA_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return

    # Mejor precio por (trip_id, type, flight_date, scrape_date)
    best = {}
    for row in rows:
        tid   = row.get("trip_id") or "default"
        key   = (tid, row["type"], row["flight_date"])
        sdate = row["date"]
        price = float(row["price_eur"])
        best.setdefault(key, {})
        if sdate not in best[key] or price < best[key][sdate]:
            best[key][sdate] = price

    # Agrupar series por trip y tipo
    trip_ids = sorted({k[0] for k in best})
    n_rows   = len(trip_ids) * 2

    BG, SURFACE, TEXT = "#0B0F14", "#141B24", "#EDEFF2"
    COLORS = ["#00FF88", "#00BBFF", "#FF6B6B", "#FFD93D", "#BB88FF", "#FF88BB"]

    fig, axes = plt.subplots(n_rows, 1, figsize=(10, 4 * n_rows), facecolor=BG, squeeze=False)
    fig.suptitle("✈  VUELOS TRACKER — Evolución de precios",
                 color=TEXT, fontsize=12, fontweight="bold")

    ax_idx = 0
    for tid in trip_ids:
        for ftype, title_base in [("outbound", "IDA"), ("return", "VUELTA")]:
            ax = axes[ax_idx][0]
            ax_idx += 1

            ax.set_facecolor(SURFACE)
            for sp in ["top", "right"]:
                ax.spines[sp].set_visible(False)
            for sp in ["bottom", "left"]:
                ax.spines[sp].set_color("#2A3A4A")
            ax.tick_params(colors=TEXT, labelsize=8)
            ax.set_title(f"{tid} — {title_base}", color=TEXT, fontsize=10, pad=6, loc="left")
            ax.set_ylabel("EUR", color=TEXT, fontsize=9)
            ax.grid(True, color="#1E2A38", linewidth=0.5, linestyle="--")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"€{x:,.0f}"))

            routes = sorted(
                (k, v) for k, v in best.items()
                if k[0] == tid and k[1] == ftype
            )
            all_dates = sorted({d for _, day_map in routes for d in day_map})

            for i, ((_tid, _typ, fdate), day_map) in enumerate(routes):
                color  = COLORS[i % len(COLORS)]
                ys     = [day_map.get(d) for d in all_dates]
                xs     = list(range(len(all_dates)))
                valid  = [(x, y) for x, y in zip(xs, ys) if y is not None]
                if not valid:
                    continue
                vx, vy = zip(*valid)
                day_num = fdate[8:10].lstrip("0")
                month   = dt.strptime(fdate, "%Y-%m-%d").strftime("%b")
                ax.plot(vx, vy, "o-", color=color, linewidth=2, markersize=7,
                        label=f"{day_num} {month}", zorder=3)
                ax.annotate(f"€{vy[-1]:,.0f}",
                            xy=(vx[-1], vy[-1]), xytext=(8, 0),
                            textcoords="offset points",
                            color=color, fontsize=9, fontweight="bold", va="center")

            if all_dates:
                ax.set_xticks(range(len(all_dates)))
                ax.set_xticklabels(
                    [dt.strptime(d, "%Y-%m-%d").strftime("%d/%m") for d in all_dates],
                    rotation=30, ha="right", color=TEXT, fontsize=8,
                )
            ax.legend(loc="upper left", facecolor=SURFACE, labelcolor=TEXT,
                      edgecolor="#2A3A4A", fontsize=9, framealpha=0.9)

    plt.tight_layout(rect=[0, 0, 1, 0.97], h_pad=2)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, facecolor=BG, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)

    send_telegram_photo(buf.getvalue())
    print("  Gráfica enviada a Telegram")


# ── Procesado de un viaje ─────────────────────────────────────────────────────

def process_trip(trip, today, history, cfg_alerts, passengers, stop_losses=None):
    trip_id   = trip["id"]
    out_cfg   = trip.get("outbound")
    ret_cfg   = trip["return"]
    pax       = passengers["adults"] + passengers.get("children", 0)

    any_google_alert = False
    any_own_alert    = False
    any_stop_loss    = False
    lines            = [f"\n<b>━━ {trip['name'].upper()} ━━</b>"]
    outbound_best    = []
    return_best      = []

    ret_origins = ",".join(ret_cfg["origins"])
    ret_dest    = ret_cfg["destination"]
    ret_max     = ret_cfg.get("max_stops")
    orig_names  = " / ".join(IATA.get(o, o) for o in ret_cfg["origins"])

    if out_cfg:
        out_origin = out_cfg["origin"]
        out_dests  = ",".join(out_cfg["destinations"])
        out_max    = out_cfg.get("max_stops")
        dest_names = " / ".join(IATA.get(d, d) for d in out_cfg["destinations"])
        stops_tag  = " (directos)" if out_max == 0 else ""

        lines.append(f"<b>── IDA{stops_tag}</b> {IATA.get(out_origin, out_origin)} → {dest_names}")

        for dep_date in out_cfg["dates"]:
            flights, insights = search_flights(
                out_origin, out_dests, dep_date,
                f"{out_origin}→Asia {dep_date}", passengers,
            )
            best = parse_best(flights, max_stops=out_max)
            if not best:
                lines.append(f"  {dep_date}: sin resultados")
                continue

            g_emoji, g_text = google_label(insights)
            typical   = insights.get("typical_price_range", [])
            typ_str   = f"€{typical[0]}–€{typical[1]}" if len(typical) == 2 else "—"
            dest_name = IATA.get(best["destination"], best["destination"])
            key       = hkey(trip_id, out_origin, best['destination'], dep_date)
            own       = own_benchmark(history, key, best["price"], cfg_alerts)

            if g_text == "BAJO":
                any_google_alert = True
            if own and own[2]:
                any_own_alert = True

            g_tag = " 🚨 <b>BAJO</b>" if g_text == "BAJO" else ""
            info_parts = []
            if g_text != "—":
                info_parts.append(f"{g_emoji} {g_text} (típico {typ_str}){g_tag}")
            info_parts.append(fmt_own(own))

            trend = predict_trend(history.get(key, []), best["price"])
            if trend:
                te, tl, tr = trend
                info_parts.append(f"{te} {tl} ({tr})")

            gf_total = gf_validate(out_origin, best["destination"], dep_date, pax, max_stops=out_max)
            if gf_total:
                gf_diff = (best["price"] - gf_total) / best["price"]
                if abs(gf_diff) >= 0.15:
                    info_parts.append(f"⚠️ GF €{int(gf_total)} ({gf_diff*100:+.0f}%)")

            sl = check_stop_loss(stop_losses, trip_id, "outbound",
                                 out_origin, best["destination"], best["price"])
            sl_line = fmt_stop_loss(sl)
            if sl_line:
                info_parts.append(sl_line)
                if sl["triggered"]:
                    any_stop_loss = True

            lines.append(
                f"\n  <b>{fmt_date(dep_date)} → {dest_name}</b>\n"
                f"  💶 {fmt_price(best['price'], pax)} · {best['stops']} esc · {best['airline']} · {fmt_duration(best['duration_m'])}\n"
                f"  {'  ·  '.join(info_parts)}"
            )
            save_record({
                "date": today, "trip_id": trip_id, "type": "outbound",
                "origin": out_origin, "destination": best["destination"],
                "flight_date": dep_date, "price_eur": best["price"],
                "stops": best["stops"], "airline": best["airline"],
                "duration_m": best["duration_m"],
                "price_level": insights.get("price_level", ""),
                "typical_low":  typical[0] if len(typical) == 2 else "",
                "typical_high": typical[1] if len(typical) == 2 else "",
            })
            outbound_best.append((dep_date, best))

    stops_tag = " (directos)" if ret_max == 0 else ""
    lines.append(f"\n<b>── VUELTA{stops_tag}</b> {orig_names} → {IATA.get(ret_dest, ret_dest)}")

    for ret_date in ret_cfg["dates"]:
        flights, insights = search_flights(
            ret_origins, ret_dest, ret_date,
            f"Asia→{ret_dest} {ret_date}", passengers,
        )
        best = parse_best(flights, max_stops=ret_max)
        if not best:
            lines.append(f"  {ret_date}: sin resultados")
            continue

        g_emoji, g_text = google_label(insights)
        typical   = insights.get("typical_price_range", [])
        typ_str   = f"€{typical[0]}–€{typical[1]}" if len(typical) == 2 else "—"
        orig_name = IATA.get(best["origin"], best["origin"])
        key       = hkey(trip_id, best['origin'], ret_dest, ret_date)
        own       = own_benchmark(history, key, best["price"], cfg_alerts)

        if g_text == "BAJO":
            any_google_alert = True
        if own and own[2]:
            any_own_alert = True

        g_tag = " 🚨 <b>BAJO</b>" if g_text == "BAJO" else ""
        info_parts = []
        if g_text != "—":
            info_parts.append(f"{g_emoji} {g_text} (típico {typ_str}){g_tag}")
        info_parts.append(fmt_own(own))

        trend = predict_trend(history.get(key, []), best["price"])
        if trend:
            te, tl, tr = trend
            info_parts.append(f"{te} {tl} ({tr})")

        gf_total = gf_validate(best["origin"], ret_dest, ret_date, pax, max_stops=ret_max)
        if gf_total:
            gf_diff = (best["price"] - gf_total) / best["price"]
            if abs(gf_diff) >= 0.15:
                info_parts.append(f"⚠️ GF €{int(gf_total)} ({gf_diff*100:+.0f}%)")

        sl = check_stop_loss(stop_losses, trip_id, "return",
                             best["origin"], ret_dest, best["price"])
        sl_line = fmt_stop_loss(sl)
        if sl_line:
            info_parts.append(sl_line)
            if sl["triggered"]:
                any_stop_loss = True

        lines.append(
            f"\n  <b>{fmt_date(ret_date)} {orig_name}→BCN</b>\n"
            f"  💶 {fmt_price(best['price'], pax)} · {best['stops']} esc · {best['airline']} · {fmt_duration(best['duration_m'])}\n"
            f"  {'  ·  '.join(info_parts)}"
        )
        save_record({
            "date": today, "trip_id": trip_id, "type": "return",
            "origin": best["origin"], "destination": ret_dest,
            "flight_date": ret_date, "price_eur": best["price"],
            "stops": best["stops"], "airline": best["airline"],
            "duration_m": best["duration_m"],
            "price_level": insights.get("price_level", ""),
            "typical_low":  typical[0] if len(typical) == 2 else "",
            "typical_high": typical[1] if len(typical) == 2 else "",
        })
        return_best.append((ret_date, best))

    if outbound_best and return_best:
        bo    = min(outbound_best, key=lambda x: x[1]["price"])
        br    = min(return_best,   key=lambda x: x[1]["price"])
        total = bo[1]["price"] + br[1]["price"]
        dn    = IATA.get(bo[1]["destination"], bo[1]["destination"])
        on    = IATA.get(br[1]["origin"],      br[1]["origin"])
        lines.append(
            f"\n💰 <b>MEJOR COMBO:</b> {IATA.get(out_origin, out_origin)}→{dn} ({fmt_date(bo[0])}) "
            f"+ {on}→{IATA.get(ret_dest, ret_dest)} ({fmt_date(br[0])}) = <b>{fmt_price(total, pax)}</b>"
        )

    return lines, any_google_alert, any_own_alert, any_stop_loss, outbound_best, return_best


# ── Combos cross-trip ─────────────────────────────────────────────────────────

def render_combo(combo, trip_results, passengers):
    pax     = passengers["adults"] + passengers.get("children", 0)
    out_res = trip_results.get(combo["outbound_trip"], {}).get("outbound", [])
    ret_res = trip_results.get(combo["return_trip"], {}).get("return", [])
    if not out_res or not ret_res:
        return []
    bo = min(out_res, key=lambda x: x[1]["price"])
    br = min(ret_res, key=lambda x: x[1]["price"])
    total = bo[1]["price"] + br[1]["price"]
    od = IATA.get(bo[1]["origin"],      bo[1]["origin"])
    dn = IATA.get(bo[1]["destination"], bo[1]["destination"])
    on = IATA.get(br[1]["origin"],      br[1]["origin"])
    rd = IATA.get(br[1]["destination"], br[1]["destination"])
    return [
        f"\n<b>━━ {combo['name'].upper()} ━━</b>",
        f"\n  <b>IDA {fmt_date(bo[0])} {od}→{dn}</b>\n"
        f"  💶 {fmt_price(bo[1]['price'], pax)} · {bo[1]['stops']} esc · {bo[1]['airline']} · {fmt_duration(bo[1]['duration_m'])}",
        f"\n  <b>VUELTA {fmt_date(br[0])} {on}→{rd}</b>\n"
        f"  💶 {fmt_price(br[1]['price'], pax)} · {br[1]['stops']} esc · {br[1]['airline']} · {fmt_duration(br[1]['duration_m'])}",
        f"\n💰 <b>TOTAL: {fmt_price(total, pax)}</b>",
    ]


# ── Date sweep (busca día más barato fuera del config) ──────────────────────

def run_date_sweeps(config, today, passengers):
    """Llama a flight-goat dates para cada ruta y devuelve líneas Telegram con
    los días más baratos en ventana ±5 si baten al mínimo configurado."""
    pax = passengers["adults"] + passengers.get("children", 0)
    lines = []
    found_better = False

    for trip in config["trips"]:
        trip_id  = trip["id"]
        out_cfg  = trip.get("outbound")
        ret_cfg  = trip["return"]

        if out_cfg:
            best = gf_dates_sweep(
                out_cfg["origin"], out_cfg["destinations"],
                out_cfg["dates"], pax,
                padding=5, max_stops=out_cfg.get("max_stops"),
            )
            if best and best["date"] not in out_cfg["dates"]:
                # comparar con mínimo del config
                cfg_min = None
                for d in out_cfg["dates"]:
                    cmp_args = ["dates", out_cfg["origin"], best["destination"],
                                "--from", d, "--to", d, "--currency", "EUR"]
                    if out_cfg.get("max_stops") == 0:
                        cmp_args += ["--stops", "non_stop"]
                    data = _run_flight_goat(cmp_args, timeout=15)
                    dates_arr = data.get("dates") if data else None
                    if dates_arr:
                        p = dates_arr[0]["price"] * pax
                        if cfg_min is None or p < cfg_min:
                            cfg_min = p
                if cfg_min and best["price_total"] < cfg_min * 0.90:
                    delta = (best["price_total"] - cfg_min) / cfg_min * 100
                    dest_name = IATA.get(best["destination"], best["destination"])
                    lines.append(
                        f"  💡 IDA <b>{fmt_date(best['date'])}</b> → {dest_name}: "
                        f"€{int(best['price_total'])} ({delta:+.0f}% vs config)"
                    )
                    save_sweep({
                        "date": today, "trip_id": trip_id, "type": "outbound",
                        "origin": out_cfg["origin"], "destination": best["destination"],
                        "config_date": ",".join(out_cfg["dates"]),
                        "best_date": best["date"],
                        "best_price_total": int(best["price_total"]),
                        "delta_vs_config_pct": f"{delta:.1f}",
                    })
                    found_better = True

        # Vuelta: iteramos cada origen (flight-goat dates es 1-a-1)
        ret_best = None
        for ret_origin in ret_cfg["origins"]:
            b = gf_dates_sweep(
                ret_origin, [ret_cfg["destination"]],
                ret_cfg["dates"], pax,
                padding=5, max_stops=ret_cfg.get("max_stops"),
            )
            if b and (ret_best is None or b["price_total"] < ret_best["price_total"]):
                b["origin"] = ret_origin
                ret_best = b
        if ret_best and ret_best["date"] not in ret_cfg["dates"]:
            cfg_min = None
            for d in ret_cfg["dates"]:
                cmp_args = ["dates", ret_best["origin"], ret_cfg["destination"],
                            "--from", d, "--to", d, "--currency", "EUR"]
                if ret_cfg.get("max_stops") == 0:
                    cmp_args += ["--stops", "non_stop"]
                data = _run_flight_goat(cmp_args, timeout=15)
                dates_arr = data.get("dates") if data else None
                if dates_arr:
                    p = dates_arr[0]["price"] * pax
                    if cfg_min is None or p < cfg_min:
                        cfg_min = p
            if cfg_min and ret_best["price_total"] < cfg_min * 0.90:
                delta = (ret_best["price_total"] - cfg_min) / cfg_min * 100
                orig_name = IATA.get(ret_best["origin"], ret_best["origin"])
                lines.append(
                    f"  💡 VUELTA <b>{fmt_date(ret_best['date'])}</b> {orig_name}→BCN: "
                    f"€{int(ret_best['price_total'])} ({delta:+.0f}% vs config)"
                )
                save_sweep({
                    "date": today, "trip_id": trip_id, "type": "return",
                    "origin": ret_best["origin"], "destination": ret_cfg["destination"],
                    "config_date": ",".join(ret_cfg["dates"]),
                    "best_date": ret_best["date"],
                    "best_price_total": int(ret_best["price_total"]),
                    "delta_vs_config_pct": f"{delta:.1f}",
                })
                found_better = True

    if found_better:
        return ["\n<b>━━ DÍAS MÁS BARATOS (±5d, fuera del config) ━━</b>"] + lines
    return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config     = load_config()
    today      = date.today().strftime("%Y-%m-%d")
    history    = load_history()
    passengers = config.get("passengers", {"adults": 2, "children": 0, "infants_on_lap": 0})
    cfg_alerts = config.get("alerts", {})

    any_google_alert = False
    any_own_alert    = False
    any_stop_loss    = False
    stop_losses      = config.get("stop_loss", [])
    lines = [f"✈️ <b>VUELOS TRACKER</b> — {today}\n"]

    trip_results = {}
    for trip in config["trips"]:
        print(f"\n[{trip['name']}]")
        t_lines, t_google, t_own, t_sl, t_out, t_ret = process_trip(
            trip, today, history, cfg_alerts, passengers, stop_losses
        )
        lines.extend(t_lines)
        trip_results[trip["id"]] = {"outbound": t_out, "return": t_ret}
        if t_google:
            any_google_alert = True
        if t_own:
            any_own_alert = True
        if t_sl:
            any_stop_loss = True

    for combo in config.get("combos", []):
        lines.extend(render_combo(combo, trip_results, passengers))

    print("\nBarrido de fechas ±5d (flight-goat)...")
    sweep_lines = run_date_sweeps(config, today, passengers)
    if sweep_lines:
        lines.extend(sweep_lines)
        print(f"  {len(sweep_lines)-1} mejoras encontradas")
    else:
        print("  Sin mejoras significativas")

    if any_google_alert or any_own_alert or any_stop_loss:
        tags = []
        if any_stop_loss:
            tags.append("STOP-LOSS disparado")
        if any_google_alert:
            tags.append("Google marca precio BAJO")
        if any_own_alert:
            tags.append("mínimo de nuestra historia")
        lines.append(f"\n🚨🚨 <b>ALERTA: {' + '.join(tags)} — revisa ya</b> 🚨🚨")

    message = "\n".join(lines)
    print("\n" + "=" * 60)
    print(message)
    print("=" * 60)

    send_telegram(message)

    print("\nGenerando gráfica...")
    generate_chart()

    print("\nActualizando repositorio...")
    import datetime
    repo = Path(__file__).parent
    today = datetime.date.today().isoformat()
    for p in ["data/prices.csv", "data/date_sweeps.csv"]:
        if (repo / p).exists():
            subprocess.run(["git", "add", p], cwd=repo, check=False)
    result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if result.returncode != 0:
        subprocess.run(["git", "commit", "-m", f"chore: update prices {today}"], cwd=repo, check=True)
        subprocess.run(["git", "push"], cwd=repo, check=True)
        print("  CSV commiteado y pusheado")
    else:
        print("  Sin cambios en el CSV")

    print("✓ Hecho")


if __name__ == "__main__":
    main()
