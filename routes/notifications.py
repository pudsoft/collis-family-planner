"""Notifications blueprint — in-app feed (/notifications) and the external
push-trigger API (/api/notify).
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

import config
from modules import notifications
from routes.utils import current_person, get_db, get_prefs

log = logging.getLogger(__name__)

bp = Blueprint("notifications", __name__)


def _friendly_ts(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%a %-d %b, %H:%M")
    except Exception:
        return iso_str or ""


@bp.route("/notifications")
def notifications_view():
    person = current_person()
    db     = get_db()
    prefs  = get_prefs(db, person)

    highlight_notif = request.args.get("notif", type=int)
    feed = notifications.get_notifications(db, person)
    for n in feed:
        n["created_display"] = _friendly_ts(n.get("created_at", ""))

    return render_template(
        "notifications.html",
        person=person,
        prefs=prefs,
        notifications=feed,
        highlight_notif=highlight_notif,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
    )


@bp.route("/notifications/<int:notif_id>/clear", methods=["POST"])
def notification_clear(notif_id: int):
    person = current_person()
    ok = notifications.clear_notification(get_db(), notif_id, person)
    if not ok:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@bp.route("/notifications/clear_all", methods=["POST"])
def notification_clear_all():
    person = current_person()
    count = notifications.clear_all_notifications(get_db(), person)
    return jsonify({"ok": True, "count": count})


# ── External trigger API ────────────────────────────────────────────────────
#
# POST /api/notify
# Header:  X-API-Key: <config.NOTIFY_API_KEY>
# Body:    {"person": "katie"|"paul"|"joshua"|"violet"|"family",
#           "title": "...", "body": "...",
#           "url": "https://... (optional)",
#           "urgency": "low"|"default"|"high"|"critical" (optional)}
#
# See scripts/notify.py for a ready-made CLI wrapper around this endpoint.

@bp.route("/api/notify", methods=["POST"])
def api_notify():
    key = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(key, config.NOTIFY_API_KEY):
        return jsonify({"error": "Invalid or missing API key"}), 401

    data = request.get_json(force=True, silent=True) or {}

    person = (data.get("person") or "").strip().lower()
    if person not in config.PEOPLE + ["family"]:
        return jsonify({"error": f"person must be one of {config.PEOPLE + ['family']}"}), 400

    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    body    = (data.get("body") or "").strip()
    url     = (data.get("url") or "").strip() or None
    urgency = (data.get("urgency") or "default").strip().lower()
    if urgency not in config.NOTIFY_URGENCY_LEVELS:
        return jsonify({"error": f"urgency must be one of {config.NOTIFY_URGENCY_LEVELS}"}), 400

    notif_id = notifications.create_notification(
        get_db(), person, title, body, url=url, urgency=urgency,
    )
    return jsonify({"ok": True, "id": notif_id})
