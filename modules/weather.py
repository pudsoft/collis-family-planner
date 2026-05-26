"""Open-Meteo weather for Brundall, Norfolk. No API key required.

Caching strategy
----------------
* Successful fetch  → cached for _CACHE_TTL (1 hour)
* Failed fetch      → back-off for _FAIL_TTL (5 minutes) then retry
* Both cases update _cache_ts immediately so the *next* request is never
  blocked — we always return stale data rather than waiting for a timeout.
"""
from __future__ import annotations

import time
import logging
import requests
from config import WEATHER_LAT, WEATHER_LON

log = logging.getLogger(__name__)

_CACHE_TTL   = 3600   # seconds before a successful response expires
_FAIL_TTL    = 300    # back-off after a failed fetch (5 minutes)
_HTTP_TIMEOUT = 5     # seconds — short so a bad API never blocks the dashboard

_cache:    dict  = {}
_cache_ts: float = 0.0   # timestamp of last successful OR failed fetch

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

_FALLBACK = {"current": {"temp": None, "desc": "Unavailable", "emoji": "❓", "wind": 0},
             "forecast": []}


def get_weather() -> dict:
    global _cache, _cache_ts

    # Use cached data if it's still fresh (success TTL or failure back-off)
    ttl = _CACHE_TTL if _cache else _FAIL_TTL
    if _cache_ts > 0 and (time.time() - _cache_ts) < ttl:
        return _cache or _FALLBACK

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

        for i, date in enumerate(dates):
            d, e = WMO_DESCRIPTIONS.get(int(codes[i]) if i < len(codes) else 0, ("?", "❓"))
            forecast.append({
                "date":     date,
                "desc":     d,
                "emoji":    e,
                "max":      round(max_temps[i], 1) if i < len(max_temps) else None,
                "min":      round(min_temps[i], 1) if i < len(min_temps) else None,
                "rain_mm":  round(rain_mm[i], 1)   if i < len(rain_mm)   else None,
                "rain_pct": int(rain_pct[i])        if i < len(rain_pct)  else None,
            })

        _cache = {
            "current": {
                "temp":  round(current.get("temperature", 0), 1),
                "desc":  desc,
                "emoji": emoji,
                "wind":  round(current.get("windspeed", 0), 1),
            },
            "forecast": forecast,
        }
        _cache_ts = time.time()   # full TTL on success
        log.debug("Weather fetched OK")
        return _cache

    except Exception as exc:
        log.warning("Weather fetch failed: %s", exc)
        # Update timestamp so we back off for _FAIL_TTL before retrying —
        # this prevents every dashboard request from blocking on a dead API.
        _cache_ts = time.time()
        return _cache or _FALLBACK
