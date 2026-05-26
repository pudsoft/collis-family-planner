"""wttr.in weather for Brundall, Norfolk. No API key required.

Caching strategy
----------------
* Weather is written to disk (weather_cache.json) after every successful fetch
  so it survives server restarts.
* On import the disk cache is loaded immediately — get_weather() is always
  instant, it never blocks a dashboard request.
* A background thread refreshes the cache every _REFRESH_INTERVAL seconds.
  If wttr.in is unavailable, the previous data is served and the thread
  retries every _FAIL_RETRY seconds without touching user requests.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import requests
from config import WEATHER_LAT, WEATHER_LON

log = logging.getLogger(__name__)

_CACHE_FILE       = Path(__file__).parent.parent / "data" / "weather_cache.json"
_REFRESH_INTERVAL = 900    # background refresh every 15 minutes
_FAIL_RETRY       = 300    # retry interval after a failed fetch (5 minutes)
_HTTP_TIMEOUT     = 15     # seconds (background thread — latency doesn't matter)

_cache:     dict  = {}
_cache_lock = threading.Lock()

# wttr.in weather codes → emoji
# Description comes directly from the API response.
_WTTR_EMOJI = {
    113: "☀️",   # Clear/Sunny
    116: "⛅",   # Partly cloudy
    119: "☁️",   # Cloudy
    122: "☁️",   # Overcast
    143: "🌫️",  # Mist
    176: "🌦️",  # Patchy rain
    179: "🌨️",  # Patchy snow
    182: "🌧️",  # Patchy sleet
    185: "🌧️",  # Patchy freezing drizzle
    200: "⛈️",  # Thundery outbreaks
    227: "❄️",  # Blowing snow
    230: "❄️",  # Blizzard
    248: "🌫️",  # Fog
    260: "🌫️",  # Freezing fog
    263: "🌦️",  # Patchy light drizzle
    266: "🌦️",  # Light drizzle
    281: "🌧️",  # Freezing drizzle
    284: "🌧️",  # Heavy freezing drizzle
    293: "🌦️",  # Patchy light rain
    296: "🌧️",  # Light rain
    299: "🌧️",  # Moderate rain at times
    302: "🌧️",  # Moderate rain
    305: "🌧️",  # Heavy rain at times
    308: "🌧️",  # Heavy rain
    311: "🌧️",  # Light freezing rain
    314: "🌧️",  # Mod/heavy freezing rain
    317: "🌧️",  # Light sleet
    320: "🌧️",  # Mod/heavy sleet
    323: "🌨️",  # Patchy light snow
    326: "🌨️",  # Light snow
    329: "❄️",  # Patchy moderate snow
    332: "❄️",  # Moderate snow
    335: "❄️",  # Patchy heavy snow
    338: "❄️",  # Heavy snow
    350: "🌨️",  # Ice pellets
    353: "🌦️",  # Light rain shower
    356: "🌧️",  # Mod/heavy rain shower
    359: "⛈️",  # Torrential rain shower
    362: "🌧️",  # Light sleet showers
    365: "🌧️",  # Mod/heavy sleet showers
    368: "🌨️",  # Light snow showers
    371: "❄️",  # Mod/heavy snow showers
    374: "🌨️",  # Light ice pellet showers
    377: "🌨️",  # Mod/heavy ice pellet showers
    386: "⛈️",  # Patchy rain with thunder
    389: "⛈️",  # Mod/heavy rain with thunder
    392: "⛈️",  # Patchy snow with thunder
    395: "⛈️",  # Mod/heavy snow with thunder
}

_FALLBACK = {
    "current":  {"temp": None, "desc": "Unavailable", "emoji": "❓", "wind": 0},
    "forecast": [],
}


# ── Disk cache ────────────────────────────────────────────────────────────────

def _load_disk() -> dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text())
    except Exception as exc:
        log.warning("weather: could not read disk cache: %s", exc)
    return {}


def _save_disk(data: dict):
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data))
    except Exception as exc:
        log.warning("weather: could not write disk cache: %s", exc)


# ── Live fetch ────────────────────────────────────────────────────────────────

def _fetch_live() -> dict | None:
    """Fetch from wttr.in. Returns a cache dict on success, None on failure."""
    url = f"https://wttr.in/{WEATHER_LAT},{WEATHER_LON}?format=j1"
    try:
        resp = requests.get(
            url, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "CollisFamilyPlanner/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()

        cur  = data["current_condition"][0]
        code = int(cur["weatherCode"])
        desc = cur["weatherDesc"][0]["value"]
        emoji = _WTTR_EMOJI.get(code, "🌡️")

        forecast = []
        for day in data.get("weather", []):
            # Use the noon (1200) hour as representative for the day
            noon = next(
                (h for h in day.get("hourly", []) if str(h.get("time")) == "1200"),
                day.get("hourly", [{}])[0],
            )
            d_code  = int(noon.get("weatherCode", 113))
            d_desc  = (noon.get("weatherDesc") or [{}])[0].get("value", "")
            d_emoji = _WTTR_EMOJI.get(d_code, "🌡️")

            # Aggregate rain across all hours
            hourly = day.get("hourly", [])
            rain_pct = max((int(h.get("chanceofrain", 0)) for h in hourly), default=0)
            rain_mm  = round(sum(float(h.get("precipMM", 0)) for h in hourly), 1)

            forecast.append({
                "date":     day["date"],
                "desc":     d_desc,
                "emoji":    d_emoji,
                "max":      float(day["maxtempC"]),
                "min":      float(day["mintempC"]),
                "rain_mm":  rain_mm,
                "rain_pct": rain_pct,
            })

        result = {
            "current": {
                "temp":  float(cur["temp_C"]),
                "desc":  desc,
                "emoji": emoji,
                "wind":  float(cur["windspeedKmph"]),
            },
            "forecast": forecast,
            "fetched_at": time.time(),
        }
        log.info("weather: fetched OK via wttr.in (%s, %s)", desc, cur["temp_C"])
        return result

    except Exception as exc:
        log.warning("weather: fetch failed: %s", exc)
        return None


# ── Background refresh loop ───────────────────────────────────────────────────

def _refresh_loop():
    while True:
        result = _fetch_live()
        if result:
            with _cache_lock:
                global _cache
                _cache = result
            _save_disk(result)
            time.sleep(_REFRESH_INTERVAL)
        else:
            time.sleep(_FAIL_RETRY)


# ── Public API ────────────────────────────────────────────────────────────────

def get_weather() -> dict:
    """Return cached weather instantly. Never blocks."""
    with _cache_lock:
        return dict(_cache) if _cache else _FALLBACK


# ── Startup: load disk cache then launch background refresh ───────────────────
_disk = _load_disk()
if _disk:
    _cache = _disk
    log.info("weather: loaded from disk cache (fetched_at=%s)",
             time.strftime("%H:%M", time.localtime(_disk.get("fetched_at", 0))))

threading.Thread(target=_refresh_loop, daemon=True, name="weather-refresh").start()
