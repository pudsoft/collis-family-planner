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


# ── Open-Meteo 5-day forecast ─────────────────────────────────────────────────

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


def _fetch_om_forecast() -> list | None:
    """5-day daily forecast from Open-Meteo. Returns list of day dicts or None."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min"
        ",precipitation_probability_max,precipitation_sum,windspeed_10m_max"
        "&timezone=Europe%2FLondon&forecast_days=5"
    )
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT,
                            headers={"User-Agent": "CollisFamilyPlanner/1.0"})
        resp.raise_for_status()
        d = resp.json()["daily"]
        result = []
        for i, date in enumerate(d["time"]):
            code = int(d["weathercode"][i] or 0)
            result.append({
                "date":     date,
                "emoji":    _WMO_EMOJI.get(code, "🌡️"),
                "desc":     _WMO_DESC.get(code, ""),
                "max":      float(d["temperature_2m_max"][i] or 0),
                "min":      float(d["temperature_2m_min"][i] or 0),
                "rain_pct": int(d["precipitation_probability_max"][i] or 0),
                "rain_mm":  round(float(d["precipitation_sum"][i] or 0), 1),
                "wind_max": round(float(d["windspeed_10m_max"][i] or 0)),
            })
        log.info("weather: Open-Meteo forecast OK (%d days)", len(result))
        return result
    except Exception as exc:
        log.warning("weather: Open-Meteo forecast failed: %s", exc)
        return None


# ── Live fetch (wttr.in current + hourly, Open-Meteo forecast) ────────────────

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

        today_hourly: list = []
        forecast = []
        for day_idx, day in enumerate(data.get("weather", [])):
            noon = next(
                (h for h in day.get("hourly", []) if str(h.get("time")) == "1200"),
                day.get("hourly", [{}])[0],
            )
            d_code  = int(noon.get("weatherCode", 113))
            d_desc  = (noon.get("weatherDesc") or [{}])[0].get("value", "")
            d_emoji = _WTTR_EMOJI.get(d_code, "🌡️")

            hourly = day.get("hourly", [])
            rain_pct = max((int(h.get("chanceofrain", 0)) for h in hourly), default=0)
            rain_mm  = round(sum(float(h.get("precipMM", 0)) for h in hourly), 1)
            wind_max = round(max((float(h.get("windspeedKmph", 0)) for h in hourly), default=0))

            if day_idx == 0:
                for h in hourly:
                    t = str(h.get("time", "0"))
                    h_code = int(h.get("weatherCode", 113))
                    today_hourly.append({
                        "time":       f"{int(t) // 100:02d}:00",
                        "temp":       round(float(h.get("tempC", 0))),
                        "feels_like": round(float(h.get("FeelsLikeC", h.get("tempC", 0)))),
                        "emoji":      _WTTR_EMOJI.get(h_code, "🌡️"),
                        "desc":       (h.get("weatherDesc") or [{}])[0].get("value", ""),
                        "wind":       round(float(h.get("windspeedKmph", 0))),
                        "rain_pct":   int(h.get("chanceofrain", 0)),
                        "rain_mm":    round(float(h.get("precipMM", 0)), 1),
                        "uv":         int(h.get("uvIndex", 0)),
                        "humidity":   int(h.get("humidity", 0)),
                    })

            forecast.append({
                "date":     day["date"],
                "desc":     d_desc,
                "emoji":    d_emoji,
                "max":      float(day["maxtempC"]),
                "min":      float(day["mintempC"]),
                "rain_mm":  rain_mm,
                "rain_pct": rain_pct,
                "wind_max": wind_max,
            })

        om_forecast = _fetch_om_forecast()

        result = {
            "current": {
                "temp":       float(cur["temp_C"]),
                "desc":       desc,
                "emoji":      emoji,
                "wind":       float(cur["windspeedKmph"]),
                "feels_like": float(cur.get("FeelsLikeC", cur["temp_C"])),
                "humidity":   int(cur.get("humidity", 0)),
                "uv":         int(cur.get("uvIndex", 0)),
                "pressure":   int(cur.get("pressure", 0)),
            },
            "forecast":     om_forecast if om_forecast else forecast,
            "today_hourly": today_hourly,
            "fetched_at":   time.time(),
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
