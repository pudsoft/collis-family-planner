"""Admin blueprint — /admin routes, /admin/smarthome/*, /network/wifi_credentials, /admin/wlan, /admin/broadcast, /admin/refresh_calendar."""
from __future__ import annotations

import json
import logging
from datetime import date

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

import config
from modules import auth, calendar_sync, hive, medicines, ntfy, unifi
from routes.utils import current_person, get_db, get_prefs, require_admin

log = logging.getLogger(__name__)

bp = Blueprint("admin", __name__)


@bp.route("/admin")
def admin_view():
    person = current_person()
    if person not in config.ADMINS:
        return redirect(url_for("settings.settings_view"))
    db = get_db()
    prefs        = get_prefs(db, person)
    chore_templates = [dict(r) for r in db.execute("SELECT * FROM chore_templates ORDER BY title").fetchall()]
    all_meds     = sorted(medicines.get_medicines(db), key=lambda m: m["name"].lower())
    _today = date.today()
    for _m in all_meds:
        _freq = _m.get("frequency_type") or "daily"
        _sched = {}
        if _m.get("dose_times"):
            try:
                _s = json.loads(_m["dose_times"])
                if isinstance(_s, dict):
                    _sched = _s
            except Exception:
                pass
        if _freq == "monthly":
            _dom = _sched.get("dom", "?")
            _m["_freq_label"] = f"Monthly (day {_dom})"
        elif _freq == "3monthly":
            _dom = _sched.get("dom", "?")
            _m["_freq_label"] = f"Every 3 months (day {_dom})"
        else:
            _m["_freq_label"] = f"{_m.get('doses_per_day') or 1}× daily"
        if _freq in ("monthly", "3monthly"):
            _nd = medicines._next_dose_date(_m, _today)
            _m["_next_due"] = _nd.isoformat() if _nd else None
        else:
            _m["_next_due"] = None
    all_devices  = [dict(r) for r in db.execute("SELECT * FROM known_devices ORDER BY display_name").fetchall()]
    google_connected = bool(
        db.execute("SELECT value FROM app_settings WHERE key='google_token'").fetchone()
    )

    pin_rows = db.execute(
        "SELECT person, login_pin FROM person_prefs WHERE person IN (?,?,?)",
        ("joshua", "violet", "family"),
    ).fetchall()
    family_passcode_row = db.execute(
        "SELECT value FROM app_settings WHERE key='family_passcode'"
    ).fetchone()
    pin_status = {r["person"]: bool(r["login_pin"]) for r in pin_rows}
    pin_status["family"] = bool(family_passcode_row and family_passcode_row["value"])

    birthdays = [dict(r) for r in db.execute("SELECT * FROM birthdays ORDER BY date_mmdd").fetchall()]

    _email_row   = db.execute("SELECT value FROM app_settings WHERE key='email_enabled'").fetchone()
    email_enabled = not (_email_row and _email_row["value"] == "0")

    vehicles = [dict(r) for r in db.execute("SELECT * FROM vehicles ORDER BY name").fetchall()]

    return render_template(
        "admin.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=True,
        all_devices=all_devices,
        chore_templates=chore_templates,
        all_meds=all_meds,
        google_connected=google_connected,
        admin_pin=config.ADMIN_PIN,
        pin_status=pin_status,
        birthdays=birthdays,
        email_enabled=email_enabled,
        vehicles=vehicles,
    )


@bp.route("/admin/chore", methods=["POST"])
@require_admin
def admin_chore_save():
    d  = request.form
    db = get_db()
    chore_id = d.get("id", type=int)

    repeat_days_raw = d.get("repeat_days", "").strip()
    repeat_days     = repeat_days_raw if repeat_days_raw else None
    interval_days   = int(d.get("interval_days") or 7)

    if chore_id:
        db.execute(
            """UPDATE chore_templates
               SET title=?, interval_days=?, default_assignee=?, active=?, repeat_days=?
               WHERE id=?""",
            (d["title"], interval_days, d["assignee"], int(d.get("active", 1)),
             repeat_days, chore_id),
        )
    else:
        db.execute(
            """INSERT INTO chore_templates (title, interval_days, default_assignee, repeat_days)
               VALUES (?,?,?,?)""",
            (d["title"], interval_days, d.get("assignee", "anyone"), repeat_days),
        )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/chore/<int:chore_id>/delete", methods=["POST"])
@require_admin
def admin_chore_delete(chore_id: int):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE chore_template_id=?", (chore_id,))
    db.execute("DELETE FROM chore_templates WHERE id=?", (chore_id,))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/set_pin", methods=["POST"])
@require_admin
def admin_set_pin():
    person  = request.form.get("person", "").strip()
    pin_val = request.form.get("pin", "").strip()
    clear   = request.form.get("clear") == "1"
    db      = get_db()

    valid_targets = list(config.PEOPLE) + ["family"]
    if person not in valid_targets:
        return jsonify({"error": "Unknown person"}), 400

    if clear:
        if person == "family":
            db.execute("DELETE FROM app_settings WHERE key='family_passcode'")
        else:
            db.execute("UPDATE person_prefs SET login_pin=NULL WHERE person=?", (person,))
        db.commit()
        return jsonify({"ok": True})

    if len(pin_val) < 4:
        return jsonify({"error": "PIN must be at least 4 digits"}), 400

    hashed = auth.hash_pin(pin_val)
    if person == "family":
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('family_passcode', ?)",
            (hashed,),
        )
    else:
        db.execute("UPDATE person_prefs SET login_pin=? WHERE person=?", (hashed, person))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/medicine", methods=["POST"])
@require_admin
def admin_medicine_save():
    d          = request.form
    db         = get_db()
    med_id     = d.get("id", type=int)
    active     = 1 if d.get("active") != "0" else 0
    freq_type  = d.get("frequency_type", "daily")

    if freq_type == "monthly":
        dom            = max(1, min(28, int(d.get("monthly_dom") or 1)))
        scheduled_time = d.get("dose_time_1", "").strip() or None
        dose_times     = json.dumps({"dom": dom, "time": scheduled_time})
        doses_per_day  = 1
    elif freq_type == "3monthly":
        dom            = max(1, min(28, int(d.get("monthly_dom") or 1)))
        scheduled_time = d.get("dose_time_1", "").strip() or None
        dose_times     = json.dumps({"dom": dom, "time": scheduled_time})
        doses_per_day  = 1
    else:
        freq_type     = "daily"
        doses_per_day = max(1, int(d.get("doses_per_day") or 1))
        raw_times      = [d.get(f"dose_time_{i}", "").strip() for i in range(1, doses_per_day + 1)]
        dose_times     = json.dumps([t or None for t in raw_times]) if doses_per_day > 1 else None
        scheduled_time = raw_times[0] if raw_times[0] else None

    also_notify = json.dumps(d.getlist("also_notify") or [])

    kwargs = dict(
        name=d["name"], person=d["person"],
        daily_dose=float(d.get("daily_dose", 1)),
        stock_count=float(d.get("stock_count", 0)),
        reorder_threshold_days=int(d.get("reorder_threshold_days", 14)),
        notes=d.get("notes") or None,
        scheduled_time=scheduled_time,
        doses_per_day=doses_per_day,
        dose_times=dose_times,
        frequency_type=freq_type,
        active=active,
        start_date=d.get("start_date") or None,
        end_date=d.get("end_date") or None,
        also_notify=also_notify,
    )
    if med_id:
        medicines.update_medicine(db, med_id, **kwargs)
    else:
        medicines.add_medicine(db, **kwargs)
    return jsonify({"ok": True})


@bp.route("/admin/medicine/<int:med_id>/toggle_active", methods=["POST"])
@require_admin
def admin_medicine_toggle_active(med_id: int):
    db  = get_db()
    row = db.execute("SELECT active FROM medicines WHERE id=?", (med_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["active"] else 1
    db.execute("UPDATE medicines SET active=? WHERE id=?", (new_val, med_id))
    db.commit()
    return jsonify({"ok": True, "active": new_val})


@bp.route("/admin/medicine/<int:med_id>/delete", methods=["POST"])
@require_admin
def admin_medicine_delete(med_id: int):
    medicines.delete_medicine(get_db(), med_id)
    return jsonify({"ok": True})


@bp.route("/admin/device", methods=["POST"])
@require_admin
def admin_device_save():
    d      = request.form
    db     = get_db()
    dev_id = d.get("id", type=int)
    if dev_id:
        db.execute(
            "UPDATE known_devices SET display_name=?, mac=?, person=?, notes=?, protected=? WHERE id=?",
            (d["display_name"], d["mac"].lower(), d.get("person"), d.get("notes"),
             1 if d.get("protected") == "1" else 0, dev_id),
        )
    else:
        db.execute(
            "INSERT OR IGNORE INTO known_devices (display_name, mac, person, notes, protected) VALUES (?,?,?,?,?)",
            (d["display_name"], d["mac"].lower(), d.get("person"), d.get("notes"),
             1 if d.get("protected") == "1" else 0),
        )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/device/<int:dev_id>/delete", methods=["POST"])
@require_admin
def admin_device_delete(dev_id: int):
    get_db().execute("DELETE FROM known_devices WHERE id=?", (dev_id,))
    get_db().commit()
    return jsonify({"ok": True})


@bp.route("/admin/device/<int:dev_id>/block", methods=["POST"])
@require_admin
def admin_device_block(dev_id: int):
    row = get_db().execute("SELECT mac FROM known_devices WHERE id=?", (dev_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    ok = unifi.block_device(row["mac"])
    return jsonify({"ok": ok})


@bp.route("/admin/device/<int:dev_id>/unblock", methods=["POST"])
@require_admin
def admin_device_unblock(dev_id: int):
    row = get_db().execute("SELECT mac FROM known_devices WHERE id=?", (dev_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    ok = unifi.unblock_device(row["mac"])
    return jsonify({"ok": ok})


@bp.route("/admin/device/<int:dev_id>/kick", methods=["POST"])
@require_admin
def admin_device_kick(dev_id: int):
    row = get_db().execute("SELECT mac FROM known_devices WHERE id=?", (dev_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    ok = unifi.kick_device(row["mac"])
    return jsonify({"ok": ok})


@bp.route("/admin/devices/protect_bulk", methods=["POST"])
@require_admin
def admin_devices_protect_bulk():
    devices = json.loads(request.form.get("devices", "[]"))
    db = get_db()
    for dev in devices:
        mac     = dev.get("mac", "").lower()
        name    = dev.get("name") or mac
        protect = 1 if dev.get("protect") else 0
        if not mac:
            continue
        existing = db.execute("SELECT id FROM known_devices WHERE mac=?", (mac,)).fetchone()
        if existing:
            db.execute("UPDATE known_devices SET protected=?, display_name=? WHERE mac=?",
                       (protect, name, mac))
        elif protect:
            db.execute(
                "INSERT INTO known_devices (display_name, mac, protected) VALUES (?,?,1)",
                (name, mac),
            )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/mac/<mac>/block", methods=["POST"])
@require_admin
def admin_mac_block(mac: str):
    ok = unifi.block_device(mac)
    return jsonify({"ok": ok})


@bp.route("/admin/mac/<mac>/unblock", methods=["POST"])
@require_admin
def admin_mac_unblock(mac: str):
    ok = unifi.unblock_device(mac)
    return jsonify({"ok": ok})


@bp.route("/network/wifi_credentials/<ssid>", methods=["POST"])
@require_admin
def wifi_credentials(ssid: str):
    creds = unifi.get_wifi_credentials(ssid)
    if not creds:
        return jsonify({"error": "Network not found"}), 404
    try:
        import qrcode, io, base64
        qr_string = f"WIFI:T:{creds['security']};S:{creds['ssid']};P:{creds['password']};;"
        img = qrcode.make(qr_string)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        creds["qr_data_url"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.warning("QR code generation failed: %s", e)
        creds["qr_data_url"] = None
    return jsonify(creds)


@bp.route("/admin/wlan/<ssid>/toggle", methods=["POST"])
@require_admin
def admin_wlan_toggle(ssid: str):
    enabled = request.form.get("enabled", "true").lower() == "true"
    ok = unifi.set_wlan_enabled(ssid, enabled)
    return jsonify({"ok": ok})


@bp.route("/admin/broadcast", methods=["POST"])
@require_admin
def admin_broadcast():
    d       = request.form
    message = d.get("message", "").strip()
    title   = d.get("title", "📢 Family Notice").strip()
    if not message:
        return jsonify({"error": "No message"}), 400
    db       = get_db()
    channels = [
        dict(r)["ntfy_channel"]
        for r in db.execute("SELECT ntfy_channel FROM person_prefs WHERE ntfy_channel IS NOT NULL AND ntfy_channel != ''").fetchall()
    ]
    count = ntfy.send_broadcast(channels, message, title=title)
    return jsonify({"ok": True, "sent": count})


@bp.route("/admin/refresh_calendar", methods=["POST"])
@require_admin
def admin_refresh_calendar():
    events = calendar_sync.fetch_events(get_db())
    return jsonify({"ok": True, "count": len(events)})


# ── Smart home admin routes ────────────────────────────────────────────────────

@bp.route("/admin/smarthome/rooms", methods=["POST"])
@require_admin
def admin_smarthome_save_room():
    db = get_db()
    d  = request.json or {}
    room_id = d.get("id")
    fields  = (
        d.get("name", "Room"),
        d.get("icon", "🏠"),
        d.get("floor", "ground"),
        int(d.get("sort_order", 0)),
        int(d.get("grid_col", 0)),
        int(d.get("grid_row", 0)),
        int(d.get("grid_col_span", 1)),
        int(d.get("grid_row_span", 1)),
        d.get("zone_color") or None,
    )
    if room_id:
        db.execute(
            "UPDATE smart_rooms SET name=?,icon=?,floor=?,sort_order=?,grid_col=?,"
            "grid_row=?,grid_col_span=?,grid_row_span=?,zone_color=? WHERE id=?",
            fields + (room_id,),
        )
    else:
        db.execute(
            "INSERT INTO smart_rooms (name,icon,floor,sort_order,grid_col,grid_row,"
            "grid_col_span,grid_row_span,zone_color) VALUES (?,?,?,?,?,?,?,?,?)",
            fields,
        )
        room_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    return jsonify({"ok": True, "id": room_id})


@bp.route("/admin/smarthome/rooms/<int:room_id>/delete", methods=["POST"])
@require_admin
def admin_smarthome_delete_room(room_id: int):
    db = get_db()
    db.execute("DELETE FROM smart_devices WHERE room_id=?", (room_id,))
    db.execute("DELETE FROM smart_rooms WHERE id=?", (room_id,))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/smarthome/discover", methods=["POST"])
@require_admin
def admin_smarthome_discover():
    """Return discovered Hive heating zones for room assignment."""
    hive_devs = []
    if config.HIVE_EMAIL:
        for z in hive.get_climate_data():
            hive_devs.append({
                "provider":    "hive",
                "device_id":   z["id"],
                "name":        z["name"],
                "device_type": z["type"],
                "online":      z.get("online", True),
            })
    return jsonify({"hive": hive_devs})


@bp.route("/admin/smarthome/assign", methods=["POST"])
@require_admin
def admin_smarthome_assign():
    """Assign (or unassign) a discovered device to a room."""
    db      = get_db()
    d       = request.json or {}
    provider    = d.get("provider")
    device_id   = d.get("device_id")
    name        = d.get("name", "Device")
    device_type = d.get("device_type", "")
    room_id     = d.get("room_id")
    ha_entity_id = d.get("ha_entity_id") or None

    existing = db.execute(
        "SELECT id FROM smart_devices WHERE provider=? AND device_id=?",
        (provider, device_id),
    ).fetchone()

    if room_id is None:
        if existing:
            db.execute("DELETE FROM smart_devices WHERE id=?", (existing["id"],))
    elif existing:
        db.execute(
            "UPDATE smart_devices SET name=?,device_type=?,room_id=?,ha_entity_id=? WHERE id=?",
            (name, device_type, room_id, ha_entity_id, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO smart_devices (provider,device_id,name,device_type,room_id,ha_entity_id)"
            " VALUES (?,?,?,?,?,?)",
            (provider, device_id, name, device_type, room_id, ha_entity_id),
        )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/smarthome/settings", methods=["POST"])
@require_admin
def admin_smarthome_settings():
    """Save global smart home settings (zone temp range, grid sizes, etc.)."""
    db = get_db()
    d  = request.json or {}
    float_keys = ("zone_temp_min", "zone_temp_max")
    int_keys   = ("sh_grid_ground_cols", "sh_grid_ground_rows",
                  "sh_grid_first_cols",  "sh_grid_first_rows")
    for key in float_keys:
        val = d.get(key)
        if val is not None:
            try:
                db.execute("INSERT OR REPLACE INTO app_settings (key,value) VALUES (?,?)",
                           (key, str(float(val))))
            except (TypeError, ValueError):
                pass
    for key in int_keys:
        val = d.get(key)
        if val is not None:
            try:
                db.execute("INSERT OR REPLACE INTO app_settings (key,value) VALUES (?,?)",
                           (key, str(int(val))))
            except (TypeError, ValueError):
                pass
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/smarthome/rooms/<int:room_id>/position", methods=["POST"])
@require_admin
def admin_smarthome_room_position(room_id: int):
    """Lightweight position-only update — used by drag-and-drop editor."""
    db = get_db()
    d  = request.json or {}
    db.execute(
        "UPDATE smart_rooms SET grid_col=?,grid_row=?,grid_col_span=?,grid_row_span=? WHERE id=?",
        (int(d.get("grid_col", 0)), int(d.get("grid_row", 0)),
         int(d.get("grid_col_span", 1)), int(d.get("grid_row_span", 1)),
         room_id),
    )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/smarthome/seed", methods=["POST"])
@require_admin
def admin_smarthome_seed():
    """Pre-populate rooms from the house floor plan. Fails if rooms already exist."""
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM smart_rooms").fetchone()[0]
    if count:
        return jsonify({"ok": False, "error": f"{count} rooms already exist — delete them first"})

    seed = [
        ("Kitchen",      "🍳", "ground", 0, 0, 1, 1),
        ("Dining Room",  "🍽️", "ground", 1, 0, 3, 1),
        ("WC",           "🚽", "ground", 0, 1, 1, 1),
        ("Lounge",       "🛋️", "ground", 1, 1, 3, 1),
        ("Bedroom 4",    "🛏️", "first",  0, 0, 1, 1),
        ("Bathroom",     "🚿", "first",  1, 0, 1, 1),
        ("Bedroom 2",    "🛏️", "first",  2, 0, 2, 1),
        ("Bedroom 3",    "🛏️", "first",  0, 1, 2, 1),
        ("Bedroom 1",    "🛏️", "first",  2, 1, 2, 1),
    ]
    for name, icon, floor, col, row, col_span, row_span in seed:
        db.execute(
            "INSERT INTO smart_rooms (name,icon,floor,sort_order,grid_col,grid_row,"
            "grid_col_span,grid_row_span) VALUES (?,?,?,0,?,?,?,?)",
            (name, icon, floor, col, row, col_span, row_span),
        )
    db.commit()
    return jsonify({"ok": True, "seeded": len(seed)})


# ── Feature toggles ───────────────────────────────────────────────────────────

_FEATURE_TOGGLE_KEYS = {"email_enabled"}

@bp.route("/admin/feature_toggle", methods=["POST"])
@require_admin
def admin_feature_toggle():
    key   = request.form.get("key", "").strip()
    value = request.form.get("value", "1").strip()
    if key not in _FEATURE_TOGGLE_KEYS:
        return jsonify({"error": "Unknown feature"}), 400
    db = get_db()
    db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()
    return jsonify({"ok": True})


# ── Birthdays ──────────────────────────────────────────────────────────────────

@bp.route("/admin/birthday", methods=["POST"])
@require_admin
def admin_birthday_save():
    d  = request.get_json(force=True)
    db = get_db()
    bday_id        = d.get("id")
    name           = (d.get("name") or "").strip()
    date_mmdd      = (d.get("date_mmdd") or "").strip()
    remind_days    = int(d.get("remind_days") or 7)
    remind_persons = json.dumps(d.get("remind_persons") or [])
    notes          = (d.get("notes") or "").strip() or None
    if not name or not date_mmdd:
        return jsonify({"error": "Name and date required"}), 400
    if bday_id:
        db.execute(
            "UPDATE birthdays SET name=?, date_mmdd=?, remind_days=?, remind_persons=?, notes=? WHERE id=?",
            (name, date_mmdd, remind_days, remind_persons, notes, bday_id)
        )
    else:
        db.execute(
            "INSERT INTO birthdays (name, date_mmdd, remind_days, remind_persons, notes) VALUES (?,?,?,?,?)",
            (name, date_mmdd, remind_days, remind_persons, notes)
        )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/birthday/<int:bday_id>/delete", methods=["POST"])
@require_admin
def admin_birthday_delete(bday_id: int):
    db = get_db()
    db.execute("DELETE FROM birthdays WHERE id=?", (bday_id,))
    db.commit()
    return jsonify({"ok": True})


# ── Vehicles ──────────────────────────────────────────────────────────────────

@bp.route("/admin/vehicle", methods=["POST"])
@require_admin
def admin_vehicle_save():
    d  = request.get_json(force=True)
    db = get_db()
    vid  = d.get("id")
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400

    def _int(key):
        try:
            return int(d[key]) if d.get(key) not in (None, "") else None
        except (TypeError, ValueError):
            return None

    fields = (
        name,
        (d.get("registration") or "").strip().upper() or None,
        d.get("mot_expiry") or None,
        d.get("last_service_date") or None,
        _int("last_service_mileage"),
        _int("service_interval_miles"),
        (d.get("notes") or "").strip() or None,
    )
    if vid:
        db.execute(
            "UPDATE vehicles SET name=?,registration=?,mot_expiry=?,last_service_date=?,"
            "last_service_mileage=?,service_interval_miles=?,notes=? WHERE id=?",
            fields + (vid,),
        )
    else:
        db.execute(
            "INSERT INTO vehicles (name,registration,mot_expiry,last_service_date,"
            "last_service_mileage,service_interval_miles,notes) VALUES (?,?,?,?,?,?,?)",
            fields,
        )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/admin/vehicle/<int:vid>/delete", methods=["POST"])
@require_admin
def admin_vehicle_delete(vid: int):
    db = get_db()
    db.execute("DELETE FROM vehicles WHERE id=?", (vid,))
    db.commit()
    return jsonify({"ok": True})
