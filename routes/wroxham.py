"""Wroxham blueprint — unlisted, password-gated interactive kid's activity page.

Deliberately outside the normal family login system (see _LOGIN_EXEMPT in
app.py) so Joshua can open it directly on his own device without needing to
be logged into CFP as a family member -- gated by its own small password
instead.
"""
from __future__ import annotations

import time as _time
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, send_from_directory, session

import config
from routes.utils import get_db

bp = Blueprint("wroxham", __name__)

MEDIA_ROOT = Path(__file__).resolve().parent.parent / "data" / "wroxham_media"
BRIEFING_FILENAME = "handler_briefing.mp4"


@bp.route("/wroxham")
def wroxham_view():
    unlocked = session.get("wroxham_unlocked", False)
    progress = {}
    if unlocked:
        db = get_db()
        rows = db.execute("SELECT item_id, value FROM wroxham_progress").fetchall()
        progress = {r["item_id"]: r["value"] for r in rows}
    briefing_available = (MEDIA_ROOT / BRIEFING_FILENAME).is_file()
    return render_template(
        "wroxham.html",
        unlocked=unlocked,
        progress=progress,
        briefing_available=briefing_available,
    )


@bp.route("/wroxham/unlock", methods=["POST"])
def wroxham_unlock():
    password = (request.form.get("password") or "").strip()
    if password != config.WROXHAM_PASSWORD:
        return render_template("wroxham.html", unlocked=False, progress={}, error="Wrong password — try again."), 401
    session["wroxham_unlocked"] = True
    db = get_db()
    rows = db.execute("SELECT item_id, value FROM wroxham_progress").fetchall()
    progress = {r["item_id"]: r["value"] for r in rows}
    briefing_available = (MEDIA_ROOT / BRIEFING_FILENAME).is_file()
    return render_template(
        "wroxham.html",
        unlocked=True,
        progress=progress,
        briefing_available=briefing_available,
    )


@bp.route("/wroxham/save", methods=["POST"])
def wroxham_save():
    if not session.get("wroxham_unlocked"):
        return jsonify({"error": "Locked"}), 403
    data = request.get_json(silent=True) or {}
    item_id = str(data.get("item_id", "")).strip()
    value = str(data.get("value", ""))
    if not item_id or len(item_id) > 100:
        return jsonify({"error": "Invalid item_id"}), 400
    db = get_db()
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    if config.DB_DRIVER == "mysql":
        db.execute(
            "INSERT INTO wroxham_progress (item_id, value, updated_at) VALUES (?, ?, ?) "
            "ON DUPLICATE KEY UPDATE value=VALUES(value), updated_at=VALUES(updated_at)",
            (item_id, value, now),
        )
    else:
        db.execute(
            "INSERT INTO wroxham_progress (item_id, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (item_id, value, now),
        )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/wroxham/video/<path:filename>")
def wroxham_video(filename):
    if not session.get("wroxham_unlocked"):
        abort(403)
    if filename != BRIEFING_FILENAME:
        abort(404)
    if not (MEDIA_ROOT / filename).is_file():
        abort(404)
    # send_from_directory/Werkzeug handles Range requests for us, so the
    # <video> tag can seek/stream instead of pulling the whole file up front.
    return send_from_directory(MEDIA_ROOT, filename, conditional=True)
