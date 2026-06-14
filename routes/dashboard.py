"""Dashboard blueprint — / (home grid) and /dashboard (today view)."""
from __future__ import annotations

import json
import logging
from datetime import date

from flask import Blueprint, render_template, request, session

import config
from modules import calendar_sync, medicines, tasks, weather
from routes.utils import auth_person, current_person, get_db, get_prefs

log = logging.getLogger(__name__)

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def home_grid():
    person   = current_person()
    viewer   = auth_person()
    db       = get_db()
    prefs    = get_prefs(db, person)
    is_admin = viewer in config.ADMINS

    all_tile_ids = {t["id"] for t in config.HOME_TILES}
    visible_raw = prefs.get("visible_pages")
    if visible_raw:
        try:
            saved_ids   = set(json.loads(visible_raw))
            # Any tile not previously in saved prefs is newly added — show it by default
            visible_ids = saved_ids | (all_tile_ids - saved_ids)
        except Exception:
            visible_ids = all_tile_ids
    else:
        visible_ids = all_tile_ids

    _email_row    = db.execute("SELECT value FROM app_settings WHERE key='email_enabled'").fetchone()
    email_enabled = not (_email_row and _email_row["value"] == "0")

    visible_tiles = [
        t for t in config.HOME_TILES
        if t["id"] in visible_ids
        and (not t.get("admin_only") or is_admin)
        and (t["id"] != "email" or email_enabled)
    ]

    return render_template(
        "home.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=is_admin,
        visible_tiles=visible_tiles,
    )


@bp.route("/dashboard")
def dashboard():
    person = current_person()
    if "person" in request.args and request.args["person"] in config.PEOPLE + ["family"]:
        person = request.args["person"]
        session["person"] = person

    db   = get_db()
    prefs = get_prefs(db, person)

    today_events       = calendar_sync.get_today_events(db, person)
    work_meetings      = calendar_sync.get_work_meetings() if person in ("paul", "family") else []
    leave_checklist    = calendar_sync.before_you_leave(db, person)
    today_tasks        = tasks.get_tasks_for_person(db, person)
    today_meds         = medicines.get_today_doses(db, person)
    wx                 = weather.get_weather()
    pollen_forecast    = weather.get_pollen_forecast() if person in ("paul", "family") else []
    childcare_alert    = calendar_sync.childcare_warning(db)
    kids_first_events  = calendar_sync.first_events_today(db, ["joshua", "violet"]) if person in ("paul", "family") else {}
    weather_days       = int(prefs.get("weather_days") or 3)

    viewer = auth_person()
    if viewer not in config.ADMINS:
        today_meds = [m for m in today_meds if m["person"] == viewer]

    today_meds.sort(key=lambda m: m.get("scheduled_time") or "99:99")

    return render_template(
        "dashboard.html",
        person=person,
        prefs=prefs,
        today_events=today_events,
        work_meetings=work_meetings,
        leave_checklist=leave_checklist,
        today_tasks=today_tasks,
        today_meds=today_meds,
        weather=wx,
        pollen_forecast=pollen_forecast,
        childcare_alert=childcare_alert,
        kids_first_events=kids_first_events,
        weather_days=weather_days,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        today=date.today().isoformat(),
        is_admin=person in config.ADMINS,
        calendar_error=calendar_sync.get_sync_error() if person in config.ADMINS else None,
    )
