#!/usr/bin/env python3
"""Viaje largo 2027: ida y vuelta para 4 pax via SerpAPI Google Flights.

Config: config_2027.json (fuente humana: vuelos-2027-condiciones.md).
Cada dia prueba UN par de fechas por grupo (rotando) para caber en 250 busquedas/mes.
Grupos 'solo_ganga' solo cada N dias y solo se reportan si bajan del umbral.
Historial: data/prices_2027.csv.  Uso: python3 tracker_2027.py [--dry-run]
"""
import csv
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent
load_dotenv(REPO / ".env")

SERPAPI_KEY = os.environ["SERPAPI_KEY"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
CONFIG = REPO / "config_2027.json"
DATA = REPO / "data" / "prices_2027.csv"
FIELDS = ["date", "grupo", "ventana", "ida", "vuelta", "origen", "destino", "tipo",
          "precio_total", "precio_pp", "escalas", "aerolinea", "duracion_ida_h", "price_level"]
DRY = "--dry-run" in sys.argv


def serp(params, retries=3):
    for i in range(retries):
        try:
            r = requests.get("https://serpapi.com/search.json",
                             params={**params, "api_key": SERPAPI_KEY}, timeout=60)
            d = r.json()
            if d.get("error") and "no results" not in d["error"].lower():
                raise RuntimeError(d["error"])
            return d
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"    retry {i+1}: {e}")
            time.sleep(2 ** (i + 1))


def searches_left():
    try:
        d = requests.get("https://serpapi.com/account.json",
                         params={"api_key": SERPAPI_KEY}, timeout=20).json()
        return int(d.get("plan_searches_left", 0)), d.get("plan_name")
    except Exception:
        return None, None


def search(cfg, g, ida, vuelta, pax):
    d = serp({
        "engine": "google_flights", "type": "1",
        "departure_id": cfg["origenes"], "arrival_id": g["destino"],
        "outbound_date": ida, "return_date": vuelta,
        "adults": cfg["pasajeros"]["adults"], "children": cfg["pasajeros"]["children"],
        "stops": "2",  # 1 escala como maximo
        "currency": "EUR", "hl": "es",
    })
    flights = d.get("best_flights", []) + d.get("other_flights", [])
    level = (d.get("price_insights") or {}).get("price_level", "")
    out = []
    for f in flights:
        legs, price = f.get("flights") or [], f.get("price")
        if not legs or not isinstance(price, (int, float)) or price <= 0:
            continue
        dur_h = f.get("total_duration", 0) / 60
        if dur_h > cfg["max_duracion_ida_h"]:
            continue  # escala incomoda
        stops = len(legs) - 1
        out.append({
            "origen": legs[0]["departure_airport"]["id"],
            "destino": legs[-1]["arrival_airport"]["id"],
            "tipo": "directo" if stops == 0 else "escala",
            "precio_total": int(price), "precio_pp": round(price / pax),
            "escalas": stops,
            "aerolinea": "/".join(dict.fromkeys(l.get("airline", "?") for l in legs)),
            "duracion_ida_h": round(dur_h, 1), "price_level": level,
        })
    return out, len(flights)


def load_hist():
    if not DATA.exists():
        return []
    with open(DATA, newline="") as f:
        return list(csv.DictReader(f))


def save(rows):
    DATA.parent.mkdir(exist_ok=True)
    new = not DATA.exists()
    with open(DATA, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def telegram(msg):
    if DRY or not TG_TOKEN or not TG_CHAT:
        return
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15)
    if not r.ok:
        print("Telegram error:", r.text)


def dm(s):  # 2027-03-18 -> 18/03
    return f"{s[8:10]}/{s[5:7]}"


def main():
    cfg = json.loads(CONFIG.read_text())
    pax = cfg["pasajeros"]["adults"] + cfg["pasajeros"]["children"]
    today = date.today()
    n = today.toordinal()
    ganga_hoy = n % cfg["ganga_cada_n_dias"] == 0
    grupos = [g for g in cfg["grupos"] if g["modo"] == "completo" or ganga_hoy]

    left, plan = searches_left()
    if left is not None and left < len(grupos):
        grupos = [g for g in grupos if g["modo"] == "completo"][:max(left, 0)]
    if not grupos:
        telegram(f"⚠️ <b>Vuelos 2027</b>: SerpAPI sin busquedas ({left} restantes, {plan}). Hoy no se mira nada.")
        sys.exit(1)

    hist = load_hist()
    rows, lines, alerts = [], [], []
    vacios, errores = 0, 0

    for g in grupos:
        pares = cfg["ventanas"][g["ventana"]]["pares"]
        k = n if g["modo"] == "completo" else n // cfg["ganga_cada_n_dias"]
        ida, vuelta = pares[k % len(pares)]
        print(f"[{g['id']}] {ida} -> {vuelta} ...", end=" ", flush=True)
        try:
            res, raw = search(cfg, g, ida, vuelta, pax)
        except Exception as e:
            errores += 1
            print("ERROR", e)
            lines.append(f"❌ {g['id']}: error {e}")
            continue
        print(f"{raw} vuelos, {len(res)} validos")
        if raw == 0:
            vacios += 1
        prev = [float(h["precio_pp"]) for h in hist if h["grupo"] == g["id"]]
        prev_min = min(prev) if prev else None

        for tipo in ("directo", "escala"):
            cand = [r for r in res if r["tipo"] == tipo]
            if not cand:
                continue
            b = min(cand, key=lambda r: r["precio_total"])
            row = {"date": today.isoformat(), "grupo": g["id"], "ventana": g["ventana"],
                   "ida": ida, "vuelta": vuelta, **b}
            rows.append(row)
            umbral = g["alerta"].get(tipo)
            bajo = umbral is not None and b["precio_pp"] < umbral
            if g["modo"] == "solo_ganga" and not bajo:
                continue
            txt = (f"{g['id']} · {dm(ida)}→{dm(vuelta)} · {b['origen']}→{b['destino']} · "
                   f"<b>{b['precio_pp']} €/p</b> ({b['precio_total']:,} €) · {tipo} · "
                   f"{b['aerolinea']} · {b['duracion_ida_h']} h").replace(",", ".")
            if prev_min and b["precio_pp"] < prev_min:
                txt += f" · 📉 mín. histórico (antes {int(prev_min)})"
            if b["price_level"] == "low":
                txt += " · Google: BAJO"
            if bajo:
                alerts.append(f"🚨 {txt} · umbral {umbral}")
            else:
                lines.append(f"• {txt}")

    if not DRY:
        save(rows)

    ok = len(grupos) - errores - vacios
    head = f"✈️ <b>VUELOS 2027</b> — {today:%d/%m} · {ok}/{len(grupos)} búsquedas con datos"
    if left is not None:
        head += f" · SerpAPI {left - len(grupos)} restantes"
    msg = "\n".join([head, ""] + alerts + ([""] if alerts else []) + lines)
    if ok == 0:
        msg += "\n\n⚠️ <b>0 resultados en todas las búsquedas: revisar el tracker</b>"
    print("\n" + msg)
    telegram(msg)

    if not DRY and rows:
        subprocess.run(["git", "add", "data/prices_2027.csv"], cwd=REPO, check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO).returncode:
            subprocess.run(["git", "commit", "-m", f"chore: precios 2027 {today}"], cwd=REPO, check=False)
            subprocess.run(["git", "push"], cwd=REPO, check=False)
    sys.exit(1 if ok == 0 else 0)


if __name__ == "__main__":
    main()
