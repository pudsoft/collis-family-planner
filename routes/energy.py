"""Energy blueprint — /energy, /energy/data."""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, render_template

import config
from routes.utils import current_person, get_db, get_prefs, _pcache_get, _pcache_set

log = logging.getLogger(__name__)

bp = Blueprint("energy", __name__)


@bp.route("/energy")
def energy_view():
    person = current_person()
    db     = get_db()
    prefs  = get_prefs(db, person)
    return render_template(
        "energy.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
    )


@bp.route("/energy/data")
def energy_data():
    # Serve cached data if fresh (5-minute TTL — data logger runs every 15 min)
    _cached = _pcache_get("energy_data", 300)
    if _cached is not None:
        return jsonify(_cached)

    TEMP_DB   = Path(__file__).parent.parent / "data" / "temperature_log.db"
    ENERGY_DB = Path(__file__).parent.parent / "data" / "energy.db"

    # ── Build shared 15-min UTC timeline ────────────────────────────────────
    _now  = datetime.utcnow().replace(second=0, microsecond=0)
    _now  = _now - timedelta(minutes=_now.minute % 15)
    _start = _now - timedelta(hours=48)
    if TEMP_DB.exists():
        _tc = sqlite3.connect(TEMP_DB)
        _tr = _tc.execute("SELECT MIN(recorded_at) FROM temperature_log").fetchone()
        _tc.close()
        if _tr and _tr[0]:
            _ts  = _tr[0].replace("Z", "").replace(" ", "T")
            _tdt = datetime.strptime(_ts[:19], "%Y-%m-%dT%H:%M:%S")
            _tdt = _tdt - timedelta(minutes=_tdt.minute % 15, seconds=_tdt.second)
            if _tdt > _start:
                _start = _tdt

    timeline: list[datetime] = []
    _t = _start
    while _t <= _now:
        timeline.append(_t)
        _t += timedelta(minutes=15)

    tl_strs = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in timeline]

    def _slot(ts_str: str) -> str:
        """Round a UTC ISO timestamp string to the nearest 15-min slot key."""
        s  = ts_str.rstrip("Z").replace(" ", "T")
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return (dt - timedelta(minutes=dt.minute % 15,
                               seconds=dt.second)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = {
        "timeline":         tl_strs,
        "outdoor_current":  None,
        "solar_current_kw": None,
        "solar_today_kwh":  None,
        "solar":            [],
        "night":            [],
        "outdoor":          [],
        "rooms":            {},
    }

    # ── Our 15-min temperature logger ────────────────────────────────────────
    if TEMP_DB.exists():
        tdb = sqlite3.connect(TEMP_DB)
        tdb.row_factory = sqlite3.Row

        row = tdb.execute(
            "SELECT temperature FROM temperature_log "
            "WHERE source='outdoor' ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        if row:
            out["outdoor_current"] = row["temperature"]

        outdoor_bkt: dict[str, float] = {}
        for r in tdb.execute(
            "SELECT recorded_at, temperature FROM temperature_log "
            "WHERE source='outdoor' AND recorded_at >= datetime('now','-48 hours') "
            "ORDER BY recorded_at"
        ):
            outdoor_bkt[_slot(r["recorded_at"])] = r["temperature"]

        room_bkt: dict[str, dict[str, dict]] = {}
        for r in tdb.execute(
            "SELECT recorded_at, name, temperature, is_heating FROM temperature_log "
            "WHERE source='hive' AND recorded_at >= datetime('now','-48 hours') "
            "ORDER BY name, recorded_at"
        ):
            room_bkt.setdefault(r["name"], {})[_slot(r["recorded_at"])] = {
                "temp": r["temperature"], "h": bool(r["is_heating"])
            }

        tdb.close()

        out["outdoor"] = [outdoor_bkt.get(t) for t in tl_strs]
        for name, bkt in room_bkt.items():
            pts = [bkt.get(t, {"temp": None, "h": False}) for t in tl_strs]
            out["rooms"][name] = {
                "temps":   [p["temp"] for p in pts],
                "heating": [p["h"]    for p in pts],
            }

    # ── Energy DB (solar, synced every 15 min from Pi) ────────────────────────
    if ENERGY_DB.exists():
        edb = sqlite3.connect(ENERGY_DB)
        edb.row_factory = sqlite3.Row

        solar_bkt: dict[str, float] = {}
        latest_kw: float | None = None
        for r in edb.execute(
            "SELECT generation_date || 'T' || start_time_UTC AS ts, "
            "       power_kw, total_yield_kwh "
            "FROM   int_smadata "
            "WHERE  generation_date >= date('now','-2 day') "
            "  AND  generation_date <  date('now') "
            "UNION ALL "
            "SELECT generation_date || 'T' || start_time_UTC AS ts, "
            "       power_kw, total_yield_kwh "
            "FROM   int_solar_today "
            "WHERE  generation_date = date('now') "
            "ORDER  BY ts"
        ):
            s = _slot(r["ts"])
            solar_bkt[s] = max(solar_bkt.get(s, 0.0), r["power_kw"] or 0.0)
            latest_kw = r["power_kw"]

        out["solar"] = [solar_bkt.get(t, 0.0) for t in tl_strs]

        if latest_kw is not None:
            out["solar_current_kw"] = latest_kw

        row = edb.execute(
            "SELECT ROUND(MAX(total_yield_kwh) - MIN(total_yield_kwh), 2) AS kwh "
            "FROM   int_solar_today WHERE generation_date = date('now')"
        ).fetchone()
        if row and row["kwh"] is not None:
            out["solar_today_kwh"] = row["kwh"]

        _isday: dict[str, int] = {}
        for r in edb.execute(
            "SELECT weather_date || 'T' || substr(weather_time_UTC,1,2) AS hr, is_day "
            "FROM   int_hourly_weather "
            "WHERE  weather_date >= date('now','-2 day') "
            "ORDER  BY weather_date, weather_time_UTC"
        ):
            _isday[r["hr"]] = r["is_day"]

        _today = date.today().isoformat()
        if not any(k.startswith(_today) for k in _isday):
            _solar_hrs: set[str] = set()
            for r in edb.execute(
                "SELECT substr(start_time_UTC,1,2) AS hr "
                "FROM   int_solar_today "
                "WHERE  generation_date=? AND power_kw > 0", (_today,)
            ):
                _solar_hrs.add(f"{_today}T{r['hr']}")
            for h in range(24):
                key = f"{_today}T{h:02d}"
                _isday[key] = 1 if key in _solar_hrs else 0

        out["night"] = [
            1 if _isday.get(ts[:13], 1) == 0 else 0
            for ts in tl_strs
        ]

        edb.close()

    # ── Floor mapping from main app DB ──────────────────────────────────────
    floor_map: dict[str, str] = {}
    try:
        mdb = get_db()
        for _r in mdb.execute(
            "SELECT sd.name AS hive_name, LOWER(COALESCE(sr.floor,'')) AS floor "
            "FROM smart_devices sd "
            "JOIN smart_rooms sr ON sd.room_id = sr.id "
            "WHERE sd.provider = 'hive'"
        ).fetchall():
            floor_map[_r["hive_name"]] = _r["floor"]
    except Exception:
        pass

    ground_rooms: dict = {}
    first_rooms:  dict = {}
    other_rooms:  dict = {}
    for _name, _data in out["rooms"].items():
        _fl = floor_map.get(_name, "")
        if _fl in ("ground", "ground floor", "gf"):
            ground_rooms[_name] = _data
        elif _fl in ("first", "first floor", "1st", "1st floor", "ff"):
            first_rooms[_name] = _data
        else:
            other_rooms[_name] = _data

    out["ground_rooms"] = ground_rooms
    out["first_rooms"]  = first_rooms
    out["other_rooms"]  = other_rooms

    # ── Day / night stats (chart window) ────────────────────────────────────
    _day_pts:   dict[str, list] = {n: [] for n in out["rooms"]}
    _night_pts: dict[str, list] = {n: [] for n in out["rooms"]}

    for _i, _ts in enumerate(tl_strs):
        _hr = int(_ts[11:13])
        for _name, _data in out["rooms"].items():
            _temp = _data["temps"][_i]
            if _temp is None:
                continue
            (_day_pts[_name] if 6 <= _hr < 21 else _night_pts[_name]).append((_temp, _ts))

    def _stat_block(pairs: list) -> dict:
        if not pairs:
            return {"min": None, "min_t": None, "max": None, "max_t": None, "avg": None}
        lo = min(pairs, key=lambda x: x[0])
        hi = max(pairs, key=lambda x: x[0])
        return {
            "min": round(lo[0], 1), "min_t": lo[1],
            "max": round(hi[0], 1), "max_t": hi[1],
            "avg": round(sum(p[0] for p in pairs) / len(pairs), 1),
        }

    out["room_stats"] = {
        _name: {
            "day":         _stat_block(_day_pts[_name]),
            "night":       _stat_block(_night_pts[_name]),
            "current_temp": next((t for t in reversed(out["rooms"][_name]["temps"]) if t is not None), None),
        }
        for _name in out["rooms"]
    }

    # ── Trend ────────────────────────────────────────────────────────────────
    for _name, _data in out["rooms"].items():
        _recent = [t for t in _data["temps"] if t is not None]
        if len(_recent) >= 2:
            _diff = _recent[-1] - _recent[-2]
            _trend = "up" if _diff > 0.05 else "down" if _diff < -0.05 else "flat"
        else:
            _trend = None
        out["room_stats"][_name]["trend"] = _trend

    # ── Shared y-axis range ──────────────────────────────────────────────────
    _all_temps = [t for _d in out["rooms"].values() for t in _d["temps"] if t is not None]
    if _all_temps:
        out["y_min"] = math.floor(min(_all_temps)) - 1
        out["y_max"] = math.ceil(max(_all_temps))  + 1
    else:
        out["y_min"] = 14
        out["y_max"] = 25

    # ── Temperature extremes ─────────────────────────────────────────────────
    _cur: dict[str, float] = {}
    for _name, _data in out["rooms"].items():
        for _t in reversed(_data["temps"]):
            if _t is not None:
                _cur[_name] = _t
                break

    _period_max: dict[str, float] = {}
    _period_min: dict[str, float] = {}
    for _name, _data in out["rooms"].items():
        _ts = [t for t in _data["temps"] if t is not None]
        if _ts:
            _period_max[_name] = max(_ts)
            _period_min[_name] = min(_ts)

    if _cur:
        _hot_now  = max(_cur, key=_cur.get)
        _cold_now = min(_cur, key=_cur.get)
        _hot_day  = max(_period_max, key=_period_max.get) if _period_max else _hot_now
        _cold_day = min(_period_min, key=_period_min.get) if _period_min else _cold_now
        out["extremes"] = {
            "hot_now":  {"room": _hot_now,  "temp": round(_cur[_hot_now],  1)},
            "cold_now": {"room": _cold_now, "temp": round(_cur[_cold_now], 1)},
            "diff_now": round(_cur[_hot_now] - _cur[_cold_now], 1),
            "hot_day":  {"room": _hot_day,  "temp": round(_period_max[_hot_day],  1)},
            "cold_day": {"room": _cold_day, "temp": round(_period_min[_cold_day], 1)},
            "diff_day": round(_period_max[_hot_day] - _period_min[_cold_day], 1),
        }
    else:
        out["extremes"] = None

    _pcache_set("energy_data", out)
    return jsonify(out)
