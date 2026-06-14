"""Smarthome blueprint — /smarthome, /smarthome/status, /smarthome/device/*, /smarthome/timeline."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

import config
from modules import hive, home_assistant as ha_module, weather
from routes.utils import (
    current_person, get_db, get_prefs, require_admin,
    _pcache_get, _pcache_set, _pcache_bust,
)

log = logging.getLogger(__name__)

bp = Blueprint("smarthome", __name__)


@bp.route("/smarthome")
def smarthome_view():
    person = current_person()
    db     = get_db()
    prefs  = get_prefs(db, person)
    rooms  = [dict(r) for r in db.execute(
        "SELECT * FROM smart_rooms ORDER BY grid_row, grid_col, sort_order"
    ).fetchall()]
    devices = [dict(r) for r in db.execute(
        "SELECT * FROM smart_devices"
    ).fetchall()]
    dev_by_room: dict[int, list] = {}
    for d in devices:
        dev_by_room.setdefault(d["room_id"], []).append(d)
    for r in rooms:
        r["devices"] = dev_by_room.get(r["id"], [])

    def _setting_float(key, default):
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        try:    return float(row["value"]) if row else default
        except: return default

    def _setting_int(key, default):
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        try:    return int(row["value"]) if row else default
        except: return default

    def _img_v(filename):
        p = Path(current_app.static_folder) / filename
        try:    return int(p.stat().st_mtime)
        except: return 0

    return render_template(
        "smarthome.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
        rooms=rooms,
        hive_configured=bool(config.HIVE_EMAIL),
        zone_temp_min=_setting_float("zone_temp_min", 17.5),
        zone_temp_max=_setting_float("zone_temp_max", 19.0),
        grid_cols_ground=_setting_int("sh_grid_ground_cols", 10),
        grid_rows_ground=_setting_int("sh_grid_ground_rows", 15),
        grid_cols_first =_setting_int("sh_grid_first_cols",  10),
        grid_rows_first =_setting_int("sh_grid_first_rows",  15),
        fp_v_ground=_img_v("images/floorplan-ground.png"),
        fp_v_first=_img_v("images/floorplan-first.png"),
        fp_v_ground_zones=_img_v("images/floorplan-ground-zones.png"),
        fp_v_first_zones=_img_v("images/floorplan-first-zones.png"),
    )


@bp.route("/smarthome/status")
def smarthome_status():
    """Live poll — returns room states with Hive + HA data merged."""
    _cached = _pcache_get("smarthome_status", 30)
    if _cached is not None:
        return jsonify(_cached)

    db = get_db()

    rooms = [dict(r) for r in db.execute(
        "SELECT * FROM smart_rooms ORDER BY grid_row, grid_col, sort_order"
    ).fetchall()]
    assignments = [dict(r) for r in db.execute(
        "SELECT * FROM smart_devices"
    ).fetchall()]

    hive_data  = hive.get_climate_data() if config.HIVE_EMAIL else []
    hive_zones = {z["id"]: z for z in hive_data}
    log.info("smarthome_status: %d hive zones, ids=%s", len(hive_zones),
             list(hive_zones.keys())[:3])

    _ha_entity_ids = [
        a["ha_entity_id"] for a in assignments
        if a.get("ha_entity_id")
    ]
    ha_states = ha_module.get_all_entity_states(_ha_entity_ids) \
        if ha_module.is_configured() and _ha_entity_ids else {}

    _TLOGDB = Path(__file__).parent.parent / "data" / "temperature_log.db"
    trend_map: dict[str, str | None] = {}
    if _TLOGDB.exists():
        try:
            _tc = sqlite3.connect(_TLOGDB)
            _rows = _tc.execute(
                "SELECT name, temperature FROM temperature_log "
                "WHERE source='hive' AND recorded_at >= datetime('now','-2 hours') "
                "ORDER BY name, recorded_at DESC"
            ).fetchall()
            _tc.close()
            _by_name: dict[str, list] = {}
            for _r in _rows:
                lst = _by_name.setdefault(_r[0], [])
                if len(lst) < 2:
                    lst.append(_r[1])
            for _name, _temps in _by_name.items():
                if len(_temps) >= 2 and _temps[0] is not None and _temps[1] is not None:
                    _diff = _temps[0] - _temps[1]
                    trend_map[_name] = "up" if _diff > 0.05 else "down" if _diff < -0.05 else "flat"
        except Exception as exc:
            log.warning("smarthome_status trend query failed: %s", exc)

    log.info("smarthome_status: %d assignments, %d rooms", len(assignments), len(rooms))
    for a in assignments[:3]:
        log.info("  device: provider=%s device_id=%s room_id=%s",
                 a.get("provider"), str(a.get("device_id",""))[:20], a.get("room_id"))

    result = []
    for room in rooms:
        room_id   = room["id"]
        room_devs = [a for a in assignments if a["room_id"] == room_id]
        hive_row  = None

        for d in room_devs:
            if d["provider"] == "hive":
                z = hive_zones.get(d["device_id"])
                log.info("  room %s: match=%s current_temp=%s type=%s",
                         room["name"], z is not None,
                         z.get("current_temp") if z else None,
                         z.get("type") if z else None)
                if z:
                    hive_row = {**z, "trend": trend_map.get(d["name"])}

        result.append({
            "id":            room_id,
            "name":          room["name"],
            "icon":          room["icon"],
            "floor":         room.get("floor", "ground"),
            "grid_col":      room["grid_col"],
            "grid_row":      room["grid_row"],
            "grid_col_span": room["grid_col_span"],
            "grid_row_span": room["grid_row_span"],
            "zone_color":    room.get("zone_color"),
            "hive":          hive_row,
        })

    wx           = weather.get_weather()
    outdoor_temp = wx.get("current", {}).get("temp")
    _out = {"rooms": result, "outdoor_temp": outdoor_temp}
    _pcache_set("smarthome_status", _out)
    return jsonify(_out)


@bp.route("/smarthome/device/<int:device_db_id>/toggle", methods=["POST"])
@require_admin
def smarthome_toggle(device_db_id: int):
    db  = get_db()
    row = db.execute("SELECT * FROM smart_devices WHERE id=?", (device_db_id,)).fetchone()
    if not row:
        return jsonify({"error": "Device not found"}), 404

    ha_eid = row["ha_entity_id"] if "ha_entity_id" in row.keys() else None
    if not ha_eid or not ha_module.is_configured():
        return jsonify({"error": "No Home Assistant entity configured for this device"}), 400

    body = request.get_json(silent=True) or {}
    desired_on = body.get("on")
    if desired_on is None:
        current = ha_module.get_entity_state(ha_eid)
        desired_on = (not current) if current is not None else True

    ok, err = ha_module.set_entity_state(ha_eid, bool(desired_on))
    if ok:
        _pcache_bust("smarthome_status")
    return jsonify({"ok": ok, "now_on": bool(desired_on), **({"error": err} if err else {})})


@bp.route("/smarthome/timeline")
def smarthome_timeline():
    """Return historical temperature readings grouped into 15-min frames."""
    if current_person() not in config.ADMINS:
        return jsonify({"error": "Admin only"}), 403

    date_str = request.args.get("date", date.today().isoformat())
    try:
        date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    _TLOGDB = Path(__file__).parent.parent / "data" / "temperature_log.db"
    if not _TLOGDB.exists():
        return jsonify({"frames": []})

    try:
        with sqlite3.connect(str(_TLOGDB)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT recorded_at, source, name, temperature, is_heating "
                "FROM temperature_log WHERE recorded_at LIKE ? ORDER BY recorded_at",
                (date_str + "%",),
            ).fetchall()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    by_minute: dict = {}
    for row in rows:
        key = row["recorded_at"][:16]
        if key not in by_minute:
            by_minute[key] = {"t": row["recorded_at"], "zones": {}}
        by_minute[key]["zones"][row["name"]] = {
            "temp":    row["temperature"],
            "heating": bool(row["is_heating"]),
            "source":  row["source"],
        }

    return jsonify({"frames": [v for _, v in sorted(by_minute.items())]})
