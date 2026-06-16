"""Open-Meteo weather for Brundall, Norfolk. No API key required.

Caching strategy
----------------
* Weather is written to disk (weather_cache.json) after every successful fetch
  so it survives server restarts.
* On import the disk cache is loaded immediately — get_weather() is always
  instant, it never blocks a dashboard request.
* A background thread refreshes the cache every _REFRESH_INTERVAL seconds.
  If the API is unavailable, the previous data is served and the thread
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


# ── WMO weather codes ────────────────────────────────────────────────────────

_WMO_EMOJI = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌦️",
    61: "🌧️", 63: "🌧️", 65: "🌧️",
    71: "🌨️", 73: "❄️", 75: "❄️", 77: "❄️",
    80: "🌦️", 81: "🌧️", 82: "🌧️",
    85: "🌨️", 86: "❄️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}
_WMO_DESC = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Heavy showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm & hail", 99: "Thunderstorm & hail",
}


# ── Live fetch (Open-Meteo — current, hourly, 5-day forecast) ────────────────

def _fetch_live() -> dict | None:
    """Single Open-Meteo call for current conditions, today's hourly, and 5-day forecast."""
    import datetime as _dt
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature"
        ",weather_code,wind_speed_10m,surface_pressure,uv_index"
        "&hourly=temperature_2m,apparent_temperature,relative_humidity_2m"
        ",precipitation_probability,precipitation,weather_code,wind_speed_10m,uv_index"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min"
        ",precipitation_probability_max,precipitation_sum,windspeed_10m_max"
        "&timezone=Europe%2FLondon&forecast_days=5"
    )
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT,
                            headers={"User-Agent": "CollisFamilyPlanner/1.0"})
        resp.raise_for_status()
        data = resp.json()

        # Current conditions
        cur      = data["current"]
        cur_code = int(cur.get("weather_code") or 0)
        current  = {
            "temp":       round(float(cur["temperature_2m"])),
            "desc":       _WMO_DESC.get(cur_code, ""),
            "emoji":      _WMO_EMOJI.get(cur_code, "🌡️"),
            "wind":       round(float(cur["wind_speed_10m"])),
            "feels_like": round(float(cur["apparent_temperature"])),
            "humidity":   int(cur["relative_humidity_2m"]),
            "uv":         int(cur.get("uv_index") or 0),
            "pressure":   int(cur.get("surface_pressure") or 0),
        }

        # Today's hourly — every 3 hours (Open-Meteo returns 1-hour resolution)
        today_str    = _dt.date.today().isoformat()
        h            = data["hourly"]
        today_hourly = []
        for i, t in enumerate(h["time"]):
            if not t.startswith(today_str):
                continue
            hour = int(t[11:13])
            if hour % 3 != 0:
                continue
            h_code = int(h["weather_code"][i] or 0)
            today_hourly.append({
                "time":       t[11:16],
                "temp":       round(float(h["temperature_2m"][i] or 0)),
                "feels_like": round(float(h["apparent_temperature"][i] or 0)),
                "emoji":      _WMO_EMOJI.get(h_code, "🌡️"),
                "desc":       _WMO_DESC.get(h_code, ""),
                "wind":       round(float(h["wind_speed_10m"][i] or 0)),
                "rain_pct":   int(h["precipitation_probability"][i] or 0),
                "rain_mm":    round(float(h["precipitation"][i] or 0), 1),
                "uv":         int(h["uv_index"][i] or 0),
                "humidity":   int(h["relative_humidity_2m"][i] or 0),
            })

        # 5-day daily forecast
        d        = data["daily"]
        forecast = []
        for i, date in enumerate(d["time"]):
            code = int(d["weathercode"][i] or 0)
            forecast.append({
                "date":     date,
                "emoji":    _WMO_EMOJI.get(code, "🌡️"),
                "desc":     _WMO_DESC.get(code, ""),
                "max":      float(d["temperature_2m_max"][i] or 0),
                "min":      float(d["temperature_2m_min"][i] or 0),
                "rain_pct": int(d["precipitation_probability_max"][i] or 0),
                "rain_mm":  round(float(d["precipitation_sum"][i] or 0), 1),
                "wind_max": round(float(d["windspeed_10m_max"][i] or 0)),
            })

        result = {
            "current":      current,
            "forecast":     forecast,
            "today_hourly": today_hourly,
            "fetched_at":   time.time(),
        }
        log.info("weather: Open-Meteo OK (%s, %s°C)", current["desc"], current["temp"])
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


# ── Pollen forecast (Open-Meteo Air Quality API — all types) ─────────────────

_POLLEN_CACHE_FILE = Path(__file__).parent.parent / "data" / "pollen_cache.json"
_pollen_cache:    list = []   # grass only — kept for dashboard compat
_pollen_by_type:  dict = {}   # {"grass": [...], "birch": [...], ...}
_pollen_lock = threading.Lock()

_POLLEN_LEVELS = [
    (0,   "Very Low",  "#4caf50"),
    (10,  "Low",       "#8bc34a"),
    (30,  "Moderate",  "#ffc107"),
    (50,  "High",      "#ff9800"),
    (100, "Very High", "#f44336"),
]

# Open-Meteo field name → (display label, emoji)
_POLLEN_TYPES: dict[str, tuple[str, str]] = {
    "grass":   ("grass_pollen",   "Grass",  "🌿"),
    "birch":   ("birch_pollen",   "Birch",  "🌲"),
    "alder":   ("alder_pollen",   "Alder",  "🌳"),
    "mugwort": ("mugwort_pollen", "Weed",   "🌾"),
}

def _pollen_level(grains: float) -> tuple[str, str]:
    label, colour = "Very Low", "#4caf50"
    for threshold, lbl, col in _POLLEN_LEVELS:
        if grains >= threshold:
            label, colour = lbl, col
    return label, colour


def _fetch_pollen_all() -> dict | None:
    """Fetch all pollen types in one Open-Meteo call. Returns {key: [day_data]} or None."""
    fields = ",".join(v[0] for v in _POLLEN_TYPES.values())
    url = (
        f"https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        f"&hourly={fields}&forecast_days=5&timezone=Europe%2FLondon"
    )
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "CollisFamilyPlanner/1.0"})
        resp.raise_for_status()
        data = resp.json()
        times = data["hourly"]["time"]

        result: dict[str, list] = {}
        for key, (api_field, _label, _emoji) in _POLLEN_TYPES.items():
            values = data["hourly"].get(api_field, [None] * len(times))
            by_date: dict[str, list] = {}
            for t, v in zip(times, values):
                d = t[:10]
                if v is not None:
                    by_date.setdefault(d, []).append(v)
            days = sorted(by_date)[:5]
            day_list = []
            for d in days:
                peak = max(by_date[d])
                lbl, colour = _pollen_level(peak)
                day_list.append({"date": d, "peak": round(peak, 1), "label": lbl, "colour": colour})
            result[key] = day_list

        log.info("pollen: fetched all types OK")
        return result
    except Exception as exc:
        log.warning("pollen: fetch failed: %s", exc)
        return None


def _pollen_refresh_loop():
    time.sleep(15)
    while True:
        result = _fetch_pollen_all()
        if result:
            with _pollen_lock:
                global _pollen_cache, _pollen_by_type
                _pollen_by_type = result
                _pollen_cache   = result.get("grass", [])
            try:
                _POLLEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                _POLLEN_CACHE_FILE.write_text(json.dumps({"by_type": result}))
            except Exception:
                pass
            time.sleep(_REFRESH_INTERVAL)
        else:
            time.sleep(_FAIL_RETRY)


def get_pollen_forecast() -> list:
    """Return cached 5-day grass pollen forecast. Never blocks."""
    with _pollen_lock:
        return list(_pollen_cache)


def get_pollen_by_type() -> dict:
    """Return cached 5-day pollen forecast keyed by type. Never blocks."""
    with _pollen_lock:
        return dict(_pollen_by_type)


# Load pollen disk cache on startup
try:
    if _POLLEN_CACHE_FILE.exists():
        stored = json.loads(_POLLEN_CACHE_FILE.read_text())
        if isinstance(stored, dict) and "by_type" in stored:
            _pollen_by_type = stored["by_type"]
            _pollen_cache   = stored["by_type"].get("grass", [])
        elif isinstance(stored, list):
            _pollen_cache = stored   # old single-type format
except Exception:
    pass

threading.Thread(target=_pollen_refresh_loop, daemon=True, name="pollen-refresh").start()
