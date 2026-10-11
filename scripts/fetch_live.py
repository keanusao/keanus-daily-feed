"""Fetch live market quotes and the Fremont weather forecast into data/live.json.

Runs hourly from .github/workflows/live-data.yml. Standard library only.
Quotes come from Yahoo Finance's chart endpoint; weather from Open-Meteo.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (compatible; keanus-daily-feed/1.0)"}

PLACE = {"name": "Fremont, CA", "lat": 37.5485, "lon": -121.9886}
INDICES = [
    ("^GSPC", "S&P 500", "num"),
    ("^DJI", "Dow Jones", "num"),
    ("^IXIC", "Nasdaq", "num"),
    ("^TNX", "10-yr Treasury", "yield"),
    ("CL=F", "WTI crude", "usd"),
    ("BTC-USD", "Bitcoin", "usd0"),
]
DEFAULT_WATCH = ["NVDA", "AAPL", "MSFT", "GOOGL", "TSLA", "AMZN"]


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fmt(value, kind):
    if kind == "yield":
        return f"{value:.2f}%"
    if kind == "usd":
        return f"${value:,.2f}"
    if kind == "usd0":
        return f"${value:,.0f}"
    return f"{value:,.2f}"


def quote(symbol, name, kind):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol) + "?range=1d&interval=1d"
    meta = get_json(url)["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        return None
    change = (price - prev) / prev * 100 if prev else 0.0
    return {
        "symbol": symbol,
        "name": name or meta.get("shortName") or symbol,
        "price": fmt(price, kind),
        "change": f"{change:+.2f}%",
        "dir": "up" if change > 0.005 else "down" if change < -0.005 else "flat",
        "time": meta.get("regularMarketTime"),
    }


WMO = [
    ((0,), "clear"), ((1, 2), "partly"), ((3,), "cloudy"), ((45, 48), "fog"),
    (tuple(range(51, 58)), "drizzle"), (tuple(range(61, 68)) + (80, 81, 82), "rain"),
    (tuple(range(71, 78)) + (85, 86), "snow"), ((95, 96, 99), "storm"),
]


def cond(code):
    for codes, label in WMO:
        if code in codes:
            return label
    return "cloudy"


def weather():
    q = urllib.parse.urlencode({
        "latitude": PLACE["lat"], "longitude": PLACE["lon"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "America/Los_Angeles",
        "past_days": 1, "forecast_days": 6,
    })
    j = get_json("https://api.open-meteo.com/v1/forecast?" + q)
    d, c = j["daily"], j["current"]
    return {
        "place": PLACE["name"], "tz": j.get("timezone", "America/Los_Angeles"),
        "current": {"temp": c["temperature_2m"], "feels": c["apparent_temperature"],
                    "hum": c["relative_humidity_2m"], "wind": c["wind_speed_10m"], "cond": cond(c["weather_code"])},
        "days": [{"date": t, "hi": round(d["temperature_2m_max"][i]), "lo": round(d["temperature_2m_min"][i]),
                  "cond": cond(d["weather_code"][i]), "pop": d["precipitation_probability_max"][i]}
                 for i, t in enumerate(d["time"])],
    }


def main():
    out_path = os.path.join(DATA, "live.json")
    old = {}
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            old = json.load(f)

    try:
        with open(os.path.join(DATA, "watchlist.json"), encoding="utf-8") as f:
            watch_syms = [s for s in json.load(f).get("symbols", []) if isinstance(s, str)][:20]
    except (OSError, ValueError):
        watch_syms = DEFAULT_WATCH

    out = {"updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    try:
        out["weather"] = weather()
    except Exception as e:  # keep the last good forecast rather than publishing nothing
        print("weather failed:", e)
        if "weather" in old:
            out["weather"] = old["weather"]

    indices, watch, times = [], [], []
    for sym, name, kind in INDICES:
        try:
            q = quote(sym, name, kind)
            if q:
                indices.append(q)
                if sym != "BTC-USD":  # crypto trades 24/7; stamp prices with the stock market's time
                    times.append(q["time"])
        except Exception as e:
            print("quote failed:", sym, e)
    for sym in watch_syms:
        try:
            q = quote(sym, "", "usd")
            if q:
                watch.append(q); times.append(q["time"])
        except Exception as e:
            print("quote failed:", sym, e)
    if indices or watch:
        latest = max(t for t in times if t)
        et = datetime.fromtimestamp(latest, ZoneInfo("America/New_York"))
        out["markets"] = {"asOf": et.strftime("%a %b %-d, %-I:%M %p ET"), "indices": indices, "watch": watch}
    elif "markets" in old:
        out["markets"] = old["markets"]

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("wrote", out_path, "indices:", len(indices), "watch:", len(watch), "weather days:", len(out.get("weather", {}).get("days", [])))


if __name__ == "__main__":
    main()
