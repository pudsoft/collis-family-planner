"""Settings blueprint — /settings, /push routes, /settings/email_accounts routes."""
from __future__ import annotations

import json
import logging

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

import config
from modules import auth, email_accounts as email_accounts_mod, ntfy, push_notif
from routes.utils import auth_person, get_db, get_prefs

log = logging.getLogger(__name__)

bp = Blueprint("settings", __name__)


@bp.route("/settings")
def settings_view():
    person = auth_person()
    db     = get_db()
    prefs  = get_prefs(db, person)
    is_admin = person in config.ADMINS

    google_connected = bool(
        db.execute("SELECT value FROM app_settings WHERE key='google_token'").fetchone()
    )

    visible_raw = prefs.get("visible_pages")
    if visible_raw:
        try:
            visible_pages = set(json.loads(visible_raw))
        except Exception:
            visible_pages = {t["id"] for t in config.HOME_TILES}
    else:
        visible_pages = {t["id"] for t in config.HOME_TILES}

    home_tiles = [t for t in config.HOME_TILES if not t.get("admin_only") or is_admin]

    return render_template(
        "settings.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=is_admin,
        google_connected=google_connected,
        vapid_public_key=push_notif.get_public_key(db),
        home_tiles=home_tiles,
        visible_pages=visible_pages,
    )


@bp.route("/settings/save", methods=["POST"])
def settings_save():
    person = auth_person()
    d      = request.form
    db     = get_db()
    db.execute(
        """UPDATE person_prefs
           SET completed_style=?, ntfy_channel=?, theme=?, weather_days=?, notif_method=?
           WHERE person=?""",
        (d.get("completed_style", "fade"), d.get("ntfy_channel", ""),
         d.get("theme", "default"), int(d.get("weather_days", 3)),
         d.get("notif_method", "ntfy"), person),
    )
    db.commit()
    return redirect(url_for("settings.settings_view"))


@bp.route("/settings/save_tiles", methods=["POST"])
def settings_save_tiles():
    person = auth_person()
    db     = get_db()
    data   = request.get_json(force=True)
    visible_pages = json.dumps(data.get("visible_pages", []))
    db.execute("UPDATE person_prefs SET visible_pages=? WHERE person=?", (visible_pages, person))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/settings/change_pin", methods=["POST"])
def settings_change_pin():
    person  = auth_person()
    pin_val = request.form.get("pin", "").strip()
    if len(pin_val) < 4:
        return jsonify({"error": "PIN must be at least 4 digits"}), 400
    if not pin_val.isdigit():
        return jsonify({"error": "PIN must be digits only"}), 400
    db = get_db()
    db.execute("UPDATE person_prefs SET login_pin=? WHERE person=?",
               (auth.hash_pin(pin_val), person))
    db.commit()
    return jsonify({"ok": True})


def _ensure_email_accounts_table(db):
    try:
        import config as _cfg
        if _cfg.DB_DRIVER == "mysql":
            db.execute("""CREATE TABLE IF NOT EXISTS email_accounts (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                person        VARCHAR(50) NOT NULL,
                label         VARCHAR(100) NOT NULL,
                email_address VARCHAR(255) NOT NULL,
                app_password  TEXT NOT NULL
            )""")
        else:
            db.execute("""CREATE TABLE IF NOT EXISTS email_accounts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                person        TEXT NOT NULL,
                label         TEXT NOT NULL,
                email_address TEXT NOT NULL,
                app_password  TEXT NOT NULL
            )""")
        db.commit()
    except Exception:
        pass


@bp.route("/settings/email_accounts")
def email_accounts_list():
    person = auth_person()
    db = get_db()
    _ensure_email_accounts_table(db)
    return jsonify(email_accounts_mod.list_accounts(db, person))


@bp.route("/settings/email_accounts/add", methods=["POST"])
def email_accounts_add():
    person = auth_person()
    label      = request.form.get("label", "").strip()
    email_addr = request.form.get("email", "").strip()
    password   = request.form.get("app_password", "").strip()
    if not label or not email_addr or not password:
        return jsonify({"error": "All fields are required"}), 400
    db = get_db()
    _ensure_email_accounts_table(db)
    try:
        email_accounts_mod.add_account(db, person, label, email_addr, password)
    except Exception as e:
        log.exception("email_accounts_add failed")
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@bp.route("/settings/email_accounts/remove/<int:account_id>", methods=["POST"])
def email_accounts_remove(account_id):
    person = auth_person()
    email_accounts_mod.remove_account(get_db(), account_id, person)
    return jsonify({"ok": True})


@bp.route("/settings/ntfy_test", methods=["POST"])
def ntfy_test():
    person = auth_person()
    db     = get_db()
    prefs  = get_prefs(db, person)
    ch     = prefs.get("ntfy_channel")
    if not ch:
        return jsonify({"error": "No NTFY channel set"}), 400
    ok = ntfy.send_ntfy(ch, "Test from Family Planner!", title="✅ NTFY Test",
                        click_url=f"{config.APP_BASE_URL}/dashboard?person={person}")
    return jsonify({"ok": ok})


# ── Web Push ───────────────────────────────────────────────────────────────────

@bp.route("/push/vapid-public-key")
def push_vapid_public_key():
    return jsonify({"key": push_notif.get_public_key(get_db())})


@bp.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    person   = auth_person()
    data     = request.get_json(force=True)
    endpoint = data.get("endpoint", "").strip()
    p256dh   = data.get("p256dh", "").strip()
    auth_key = data.get("auth", "").strip()
    if not (endpoint and p256dh and auth_key):
        return jsonify({"error": "Missing fields"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
    if existing:
        db.execute("UPDATE push_subscriptions SET person=?, p256dh=?, auth=? WHERE endpoint=?",
                   (person, p256dh, auth_key, endpoint))
    else:
        db.execute("INSERT INTO push_subscriptions (person, endpoint, p256dh, auth) VALUES (?,?,?,?)",
                   (person, endpoint, p256dh, auth_key))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    data     = request.get_json(force=True)
    endpoint = data.get("endpoint", "").strip()
    if endpoint:
        db = get_db()
        db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        db.commit()
    return jsonify({"ok": True})


@bp.route("/push/test", methods=["POST"])
def push_test():
    person = auth_person()
    sent   = push_notif.send_push_to_person(
        get_db(), person,
        title="✅ Push Test",
        body="Family Planner push notifications are working!",
        url=f"{config.APP_BASE_URL}/dashboard?person={person}",
    )
    return jsonify({"ok": True, "sent": sent})
