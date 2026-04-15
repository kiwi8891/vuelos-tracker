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
from datetime import date
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
}


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── Histórico ─────────────────────────────────────────────────────────────────

def load_history():
    if not DATA_FILE.exists():
        return {}
    history = {}
    with open(DATA_FILE, newline="") as f:
        for row in csv.DictReader(f):
            key = f"{row['origin']}-{row['destination']}-{row['flight_date']}"
            history.setdefault(key, []).append(float(row["price_eur"]))
    return history


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

def search_flights(departure_id, arrival_id, outbound_date, label, passengers):
    print(f"  Buscando {label}...", end=" ", flush=True)
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
        results  = GoogleSearch(params).get_dict()
        flights  = results.get("best_flights", []) + results.get("other_flights", [])
        insights = results.get("price_insights", {})
        print(f"OK ({len(flights)} vuelos)")
        return flights, insights
    except Exception as e:
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


def fmt_own(result):
    if result is None:
        return "📊 media propia: acumulando datos…"
    avg, diff_pct, is_alert = result
    sign  = "▼" if diff_pct < 0 else "▲"
    badge = " 🚨 <b>MÍNIMO PROPIO</b>" if is_alert else ""
    return f"📊 media propia: €{avg:.0f} ({sign}{abs(diff_pct)*100:.1f}%){badge}"


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

def process_trip(trip, today, history, cfg_alerts, passengers):
    trip_id   = trip["id"]
    out_cfg   = trip["outbound"]
    ret_cfg   = trip["return"]

    any_google_alert = False
    any_own_alert    = False
    lines            = [f"\n<b>━━ {trip['name'].upper()} ━━</b>"]
    outbound_best    = []
    return_best      = []

    out_origin  = out_cfg["origin"]
    out_dests   = ",".join(out_cfg["destinations"])
    out_max     = out_cfg.get("max_stops")
    ret_origins = ",".join(ret_cfg["origins"])
    ret_dest    = ret_cfg["destination"]
    ret_max     = ret_cfg.get("max_stops")

    dest_names = " / ".join(IATA.get(d, d) for d in out_cfg["destinations"])
    orig_names = " / ".join(IATA.get(o, o) for o in ret_cfg["origins"])
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
        key       = f"{out_origin}-{best['destination']}-{dep_date}"
        own       = own_benchmark(history, key, best["price"], cfg_alerts)

        if g_text == "BAJO":
            any_google_alert = True
        if own and own[2]:
            any_own_alert = True

        g_tag = " 🚨 <b>GOOGLE: PRECIO BAJO</b>" if g_text == "BAJO" else ""
        lines.append(
            f"\n  <b>{dep_date} → {dest_name}</b>\n"
            f"  💶 €{best['price']} | {best['stops']} esc | {best['airline']} | {fmt_duration(best['duration_m'])}\n"
            f"  {g_emoji} Google: {g_text} (típico {typ_str}){g_tag}\n"
            f"  {fmt_own(own)}"
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
        key       = f"{best['origin']}-{ret_dest}-{ret_date}"
        own       = own_benchmark(history, key, best["price"], cfg_alerts)

        if g_text == "BAJO":
            any_google_alert = True
        if own and own[2]:
            any_own_alert = True

        g_tag = " 🚨 <b>GOOGLE: PRECIO BAJO</b>" if g_text == "BAJO" else ""
        lines.append(
            f"\n  <b>{ret_date} {orig_name}</b>\n"
            f"  💶 €{best['price']} | {best['stops']} esc | {best['airline']} | {fmt_duration(best['duration_m'])}\n"
            f"  {g_emoji} Google: {g_text} (típico {typ_str}){g_tag}\n"
            f"  {fmt_own(own)}"
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
            f"\n💰 <b>MEJOR COMBO:</b> {IATA.get(out_origin, out_origin)}→{dn} ({bo[0]}) "
            f"+ {on}→{IATA.get(ret_dest, ret_dest)} ({br[0]}) = <b>€{total}</b>"
        )

    return lines, any_google_alert, any_own_alert


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config     = load_config()
    today      = date.today().strftime("%Y-%m-%d")
    history    = load_history()
    passengers = config.get("passengers", {"adults": 2, "children": 0, "infants_on_lap": 0})
    cfg_alerts = config.get("alerts", {})

    any_google_alert = False
    any_own_alert    = False
    lines = [f"✈️ <b>VUELOS TRACKER</b> — {today}\n"]

    for trip in config["trips"]:
        print(f"\n[{trip['name']}]")
        t_lines, t_google, t_own = process_trip(
            trip, today, history, cfg_alerts, passengers
        )
        lines.extend(t_lines)
        if t_google:
            any_google_alert = True
        if t_own:
            any_own_alert = True

    if any_google_alert or any_own_alert:
        tags = []
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

    print("✓ Hecho")


if __name__ == "__main__":
    main()
