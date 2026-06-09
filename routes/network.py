"""Network blueprint — /network routes."""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

import config
from modules import unifi
from routes.utils import current_person, get_db, get_prefs, _pcache_get, _pcache_set

log = logging.getLogger(__name__)

bp = Blueprint("network", __name__)


@bp.route("/network")
def network_view():
    person = current_person()
    if person not in config.ADMINS:
        return redirect(url_for("settings.settings_view"))
    db         = get_db()
    prefs      = get_prefs(db, person)
    known_devs = [dict(r) for r in db.execute(
        "SELECT * FROM known_devices WHERE protected=0 ORDER BY person, display_name"
    ).fetchall()]
    return render_template(
        "network.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=True,
        known_devices=known_devs,
    )


@bp.route("/network/status")
def network_status():
    """Live poll: returns current WiFi states + connected clients + blocked/protected MACs + presence."""
    if current_person() not in config.ADMINS:
        return jsonify({"error": "Admin only"}), 403

    _cached = _pcache_get("network_status", 30)
    if _cached is not None:
        return jsonify(_cached)

    db = get_db()
    protected_macs = [r["mac"].lower() for r in db.execute(
        "SELECT mac FROM known_devices WHERE protected=1"
    ).fetchall()]
    _out = {
        "wlans":          unifi.list_wlans(),
        "blocked_macs":   list(unifi.list_blocked_macs()),
        "protected_macs": protected_macs,
    }
    _pcache_set("network_status", _out)
    return jsonify(_out)
