"""Weather page blueprint — /weather (detailed forecast for Brundall, Norfolk)."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, render_template

import config
from modules import weather
from routes.utils import auth_person, current_person, get_db, get_prefs

bp = Blueprint("weather_page", __name__)

_POLLEN_TYPE_META = [
    ("grass",   "🌿", "Grass"),
    ("birch",   "🌲", "Birch"),
    ("alder",   "🌳", "Alder"),
    ("mugwort", "🌾", "Weed"),
]


@bp.route("/weather")
def weather_view():
    person = current_person()
    viewer = auth_person()
    db     = get_db()
    prefs  = get_prefs(db, person)

    wx = weather.get_weather()

    # Mark hourly slots as current/past relative to now
    now_hour = datetime.now().hour
    today_hourly = []
    for h in wx.get("today_hourly", []):
        slot_hour = int(h["time"].split(":")[0])
        entry = dict(h)
        entry["is_current"] = (now_hour >= slot_hour) and (now_hour < slot_hour + 3)
        entry["is_past"]    = now_hour >= slot_hour + 3
        today_hourly.append(entry)

    # Derive today's high/low from wttr.in hourly data so they match the hourly strip
    today_temps = [h["temp"] for h in today_hourly]
    today_high = max(today_temps) if today_temps else None
    today_low  = min(today_temps) if today_temps else None

    # Build pollen grid: list of {date, grass, birch, alder, mugwort}
    pollen_raw = weather.get_pollen_by_type()
    grass_days = pollen_raw.get("grass", [])
    pollen_grid = []
    for idx, grass_day in enumerate(grass_days[:5]):
        row = {"date": grass_day["date"]}
        for key, _icon, _label in _POLLEN_TYPE_META:
            type_days = pollen_raw.get(key, [])
            row[key] = type_days[idx] if idx < len(type_days) else None
        pollen_grid.append(row)

    return render_template(
        "weather.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=viewer in config.ADMINS,
        weather=wx,
        today_hourly=today_hourly,
        today_high=today_high,
        today_low=today_low,
        pollen_grid=pollen_grid,
        pollen_type_meta=_POLLEN_TYPE_META,
    )
