"""Medicines blueprint — /medicines, /prn routes."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from flask import Blueprint, jsonify, render_template, request, session

import config
from modules import medicines, ntfy
from routes.utils import auth_person, current_person, get_db, get_prefs

log = logging.getLogger(__name__)

bp = Blueprint("medicines", __name__)


@bp.route("/medicines")
def medicines_view():
    person = current_person()
    if "person" in request.args and request.args["person"] in config.PEOPLE + ["family"]:
        person = request.args["person"]
        session["person"] = person

    db    = get_db()
    prefs = get_prefs(db, person)

    today = date.today()
    try:
        view_date = date.fromisoformat(request.args.get("date", "")) if request.args.get("date") else today
        if view_date > today:
            view_date = today
    except ValueError:
        view_date = today

    is_today = (view_date == today)
    if is_today:
        all_meds = medicines.get_today_doses(db, None)
    else:
        all_meds = medicines.get_doses_for_date(db, view_date.isoformat())

    def _next_slot_time(m):
        for s in m.get("dose_slots", []):
            if s.get("is_due") and not s.get("taken") and s.get("scheduled_time"):
                return s["scheduled_time"]
        return "99:99"

    all_meds.sort(key=lambda m: (0 if m["person"] == person else 1, m["person"], _next_slot_time(m), m["name"]))

    # Non-admins (Joshua, Violet) only ever see their own medicines.
    # "family" shared login and named admins can see everyone's medicines.
    viewer       = auth_person()
    viewer_admin = viewer in config.ADMINS or viewer == "family"
    if not viewer_admin:
        all_meds = [m for m in all_meds if m["person"] == viewer]

    prev_date = (view_date - timedelta(days=1)).isoformat()
    next_date = (view_date + timedelta(days=1)).isoformat() if not is_today else None
    prn_log   = medicines.get_prn_log(db, person, limit=10)

    return render_template(
        "medicines.html",
        person=person,
        prefs=prefs,
        meds=all_meds,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
        can_reorder=viewer_admin,
        prn_log=prn_log,
        view_date=view_date.isoformat(),
        view_date_label=("Today" if is_today else view_date.strftime("%-d %B %Y")),
        is_today=is_today,
        prev_date=prev_date,
        next_date=next_date,
        today=date.today().isoformat(),
    )


def _fire_also_notify(db, med_id: int, taker: str):
    med = db.execute("SELECT name, also_notify FROM medicines WHERE id=?", (med_id,)).fetchone()
    if not med:
        return
    also = []
    if med["also_notify"]:
        try:
            also = json.loads(med["also_notify"])
        except (json.JSONDecodeError, TypeError):
            pass
    if not also:
        return
    taker_display = config.PERSON_DISPLAY.get(taker, {}).get("label", taker.title())
    for recipient in also:
        if recipient == taker:
            continue
        row = db.execute(
            "SELECT ntfy_channel FROM person_prefs WHERE person=?", (recipient,)
        ).fetchone()
        if row and row["ntfy_channel"]:
            ntfy.send_medicine_taken_notification(
                channel=row["ntfy_channel"],
                taker=taker,
                taker_display=taker_display,
                medicine_name=med["name"],
            )


@bp.route("/medicines/<int:med_id>/take", methods=["POST"])
def medicine_take(med_id: int):
    person      = current_person()
    dose_date   = request.form.get("dose_date") or None
    dose_number = int(request.form.get("dose_number") or 1)
    db    = get_db()
    taken = medicines.log_dose(db, med_id, person, dose_date=dose_date, dose_number=dose_number)
    if taken:
        _fire_also_notify(db, med_id, person)
    return jsonify({"ok": True, "already_taken": not taken})


@bp.route("/medicines/doses_for_date")
def medicines_doses_for_date():
    d = request.args.get("date", "").strip()
    if not d:
        return jsonify([])
    meds = medicines.get_doses_for_date(get_db(), d)
    return jsonify(meds)


@bp.route("/medicines/<int:med_id>/untake", methods=["POST"])
def medicine_untake(med_id: int):
    dose_date   = request.form.get("dose_date") or None
    dose_number = int(request.form.get("dose_number") or 1)
    medicines.unlog_dose(get_db(), med_id, dose_date=dose_date, dose_number=dose_number)
    return jsonify({"ok": True})


@bp.route("/medicines/<int:med_id>/reordered", methods=["POST"])
def medicine_reordered(med_id: int):
    if auth_person() not in config.ADMINS and auth_person() != "family":
        return jsonify({"error": "Admin only"}), 403
    medicines.mark_reordered(get_db(), med_id)
    return jsonify({"ok": True})


@bp.route("/medicines/<int:med_id>/add_stock", methods=["POST"])
def medicine_add_stock(med_id: int):
    if auth_person() not in config.ADMINS and auth_person() != "family":
        return jsonify({"error": "Admin only"}), 403
    quantity = request.form.get("quantity", type=float)
    if not quantity:
        return jsonify({"error": "Quantity required"}), 400
    medicines.add_stock(get_db(), med_id, quantity)
    return jsonify({"ok": True})


@bp.route("/medicines/<int:med_id>/end_date", methods=["POST"])
def medicine_set_end_date(med_id: int):
    if auth_person() not in config.ADMINS and auth_person() != "family":
        return jsonify({"error": "Admin only"}), 403
    end_date = request.form.get("end_date") or None
    db = get_db()
    db.execute("UPDATE medicines SET end_date=? WHERE id=?", (end_date, med_id))
    db.commit()
    return jsonify({"ok": True})


# ── PRN ────────────────────────────────────────────────────────────────────────

@bp.route("/prn/log", methods=["POST"])
def prn_log():
    person   = request.form.get("person") or current_person()
    prn_type = request.form.get("type")
    value    = request.form.get("value", type=float)
    if prn_type not in ("paracetamol", "ibuprofen", "temperature"):
        return jsonify({"ok": False, "error": "Invalid type"}), 400
    if prn_type in ("paracetamol", "ibuprofen"):
        status = medicines.get_prn_status(get_db(), person).get(prn_type, {})
        if not status.get("safe_now", True):
            reason = "Daily limit reached" if status.get("max_reached") else f"Next dose allowed at {status.get('next_safe_at', '?')}"
            return jsonify({"ok": False, "error": reason}), 400
    medicines.log_prn(get_db(), person, prn_type, value)
    return jsonify({"ok": True})


@bp.route("/prn/<int:entry_id>/delete", methods=["POST"])
def prn_delete(entry_id: int):
    medicines.delete_prn(get_db(), entry_id)
    return jsonify({"ok": True})


@bp.route("/prn/status")
def prn_status():
    person = request.args.get("person") or current_person()
    return jsonify(medicines.get_prn_status(get_db(), person))


@bp.route("/prn/recent")
def prn_recent():
    person = request.args.get("person") or current_person()
    rows   = medicines.get_prn_log(get_db(), person)
    return jsonify(rows)
