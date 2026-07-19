"""Wroxham blueprint — unlisted, password-gated interactive kid's activity page.

Deliberately outside the normal family login system (see _LOGIN_EXEMPT in
app.py) so Joshua can open it directly on his own device without needing to
be logged into CFP as a family member -- gated by its own small password
instead.
"""
from __future__ import annotations

import time as _time

from flask import Blueprint, jsonify, render_template, request, session

import config
from routes.utils import get_db

bp = Blueprint("wroxham", __name__)


@bp.route("/wroxham")
def wroxham_view():
    unlocked = session.get("wroxham_unlocked", False)
    progress = {}
    if unlocked:
        db = get_db()
        rows = db.execute("SELECT item_id, value FROM wroxham_progress").fetchall()
        progress = {r["item_id"]: r["value"] for r in rows}
    return render_template("wroxham.html", unlocked=unlocked, progress=progress)


@bp.route("/wroxham/unlock", methods=["POST"])
def wroxham_unlock():
    password = (request.form.get("password") or "").strip()
    if password != config.WROXHAM_PASSWORD:
        return render_template("wroxham.html", unlocked=False, progress={}, error="Wrong password — try again."), 401
    session["wroxham_unlocked"] = True
    db = get_db()
    rows = db.execute("SELECT item_id, value FROM wroxham_progress").fetchall()
    progress = {r["item_id"]: r["value"] for r in rows}
    return render_template("wroxham.html", unlocked=True, progress=progress)


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
