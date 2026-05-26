"""Open-Meteo weather for Brundall, Norfolk. No API key required.

Caching strategy
----------------
* Weather is written to disk (weather_cache.json) after every successful fetch
  so it survives server restarts.
* On import the disk cache is loaded immediately — get_weather() is always
  instant, it never blocks a dashboard request.
* A background thread refreshes the cache every _REFRESH_INTERVAL seconds.
  If Open-Meteo is unavailable, the previous data is served and the thread
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

_CACHE_FILE      = Path(__file__).parent.parent / "data" / "weather_cache.json"
_REFRESH_INTERVAL = 3600   # background refresh every hour
_FAIL_RETRY       = 300    # retry interval after a failed fetch (5 minutes)
_HTTP_TIMEOUT     = 5      # seconds

_cache:     dict  = {}
_cache_lock = threading.Lock()

WMO_DESCRIPTIONS = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"),
    48: ("Icy fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌧️"),
    55: ("Heavy drizzle", "🌧️"),
    61: ("Slight rain", "🌧️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    71: ("Slight snow", "🌨️"),
    73: ("Snow", "❄️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Showers", "🌦️"),
    81: ("Rain showers", "🌧️"),
    82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "🌨️"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm + hail", "⛈️"),
    99: ("Thunderstorm + heavy hail", "⛈️"),
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
    """Fetch from Open-Meteo. Returns a cache dict on success, None on failure."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,"
        f"precipitation_sum,precipitation_probability_max"
        f"&current_weather=true"
        f"&timezone=Europe%2FLondon"
        f"&forecast_days=7"
    )
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data    = resp.json()
        current = data.get("current_weather", {})
        daily   = data.get("daily", {})

        wmo = int(current.get("weathercode", 0))
        desc, emoji = WMO_DESCRIPTIONS.get(wmo, ("Unknown", "❓"))

        forecast  = []
        dates     = daily.get("time", [])
        codes     = daily.get("weathercode", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        rain_mm   = daily.get("precipitation_sum", [])
        rain_pct  = daily.get("precipitation_probability_max", [])

        for i, d in enumerate(dates):
            dc, ec = WMO_DESCRIPTIONS.get(int(codes[i]) if i < len(codes) else 0, ("?", "❓"))
            forecast.append({
                "date":     d,
                "desc":     dc,
                "emoji":    ec,
                "max":      round(max_temps[i], 1) if i < len(max_temps) else None,
                "min":      round(min_temps[i], 1) if i < len(min_temps) else None,
                "rain_mm":  round(rain_mm[i], 1)   if i < len(rain_mm)   else None,
                "rain_pct": int(rain_pct[i])        if i < len(rain_pct)  else None,
            })

        result = {
            "current": {
                "temp":  round(current.get("temperature", 0), 1),
                "desc":  desc,
                "emoji": emoji,
                "wind":  round(current.get("windspeed", 0), 1),
            },
            "forecast": forecast,
            "fetched_at": time.time(),
        }
        log.debug("weather: fetched OK")
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
