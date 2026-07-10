"""Calendar blueprint — /calendar, /work_calendar, /calendar/auth, /calendar/oauth2callback, /api/work_meetings, event tasks, birthdays."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from flask import Blueprint, jsonify, render_template, request, session

import config
from modules import calendar_sync
from routes.utils import auth_person, current_person, get_db, get_prefs

log = logging.getLogger(__name__)

bp = Blueprint("calendar", __name__)


@bp.route("/calendar")
def calendar_view():
    person = current_person()
    if "person" in request.args and request.args["person"] in config.PEOPLE + ["family"]:
        person = request.args["person"]
        session["person"] = person

    highlight_event = request.args.get("event")
    db      = get_db()
    prefs   = get_prefs(db, person)
    events  = calendar_sync.get_cached_events(db, person)
    meetings = calendar_sync.get_work_meetings() if person in ("paul", "family") else []

    for m in meetings:
        m["_status"] = calendar_sync.meeting_status(m)

    # Inject future work meetings into the event stream so they appear inline
    if person in ("paul", "family"):
        for m in calendar_sync.get_future_work_meetings():
            events.append({
                "id":           f"wm_{m['start']}_{m['title']}",
                "title":        m["title"],
                "start_dt":     m["start"],
                "end_dt":       m.get("end", ""),
                "colour":       "peacock",
                "all_day":      False,
                "cancelled":    False,
                "attendees":    ["paul"],
                "first_seen_at": None,
            })

    # Filter to today onwards — keep events where start or end is >= today
    _today_iso = date.today().isoformat()
    events = [
        e for e in events
        if (e.get("end_dt") or e.get("start_dt", ""))[:10] >= _today_iso
    ]
    # Inject birthdays as synthetic events
    _bday_rows = db.execute("SELECT * FROM birthdays ORDER BY date_mmdd").fetchall()
    _today = date.today()
    for _b in _bday_rows:
        for _yr in [_today.year, _today.year + 1]:
            try:
                mm, dd = _b["date_mmdd"].split("-")
                bday_date = date(_yr, int(mm), int(dd))
            except Exception:
                continue
            if bday_date.isoformat() < _today_iso:
                continue
            events.append({
                "id":            f"bday_{_b['id']}_{_yr}",
                "title":         f"🎂 {_b['name']}'s Birthday",
                "start_dt":      bday_date.isoformat() + "T00:00:00",
                "end_dt":        bday_date.isoformat() + "T23:59:00",
                "colour":        "birthday",
                "all_day":       True,
                "cancelled":     False,
                "attendees":     [],
                "first_seen_at": None,
                "is_birthday":   True,
            })

    events.sort(key=lambda e: e["start_dt"])

    # Pre-load event task counts for rendering badges
    task_counts = {}
    if events:
        real_event_ids = [e["id"] for e in events if not str(e["id"]).startswith("bday_") and not str(e["id"]).startswith("wm_")]
        if real_event_ids:
            placeholders = ",".join(["?" for _ in real_event_ids])
            rows = db.execute(
                f"SELECT event_id, COUNT(*) as total, SUM(CASE WHEN completed=0 THEN 1 ELSE 0 END) as open "
                f"FROM event_tasks WHERE event_id IN ({placeholders}) GROUP BY event_id",
                real_event_ids
            ).fetchall()
            for r in rows:
                task_counts[r["event_id"]] = {"total": r["total"], "open": r["open"]}

    return render_template(
        "calendar.html",
        person=person,
        prefs=prefs,
        events=events,
        work_meetings=meetings,
        highlight_event=highlight_event,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
        task_counts=task_counts,
    )


@bp.route("/work_calendar", methods=["POST"])
def push_work_calendar():
    """Accept Paul's work meetings pushed as JSON from his work PC."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of meetings"}), 400
    count = calendar_sync.push_work_meetings(data)
    return jsonify({"ok": True, "count": count})



@bp.route("/api/work_meetings")
def api_work_meetings():
    meetings = calendar_sync.get_work_meetings()
    for m in meetings:
        m["_status"] = calendar_sync.meeting_status(m)
    return jsonify(meetings)


# ── Event tasks ────────────────────────────────────────────────────────────────

@bp.route("/calendar/tasks")
def event_tasks_list():
    event_id = request.args.get("event_id", "")
    if not event_id:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        "SELECT * FROM event_tasks WHERE event_id=? ORDER BY id", (event_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/calendar/tasks/add", methods=["POST"])
def event_tasks_add():
    data = request.get_json(force=True)
    event_id = (data.get("event_id") or "").strip()
    title    = (data.get("title") or "").strip()
    assignee = (data.get("assignee") or "anyone").strip()
    if not event_id or not title:
        return jsonify({"error": "Missing fields"}), 400
    person = auth_person()
    db = get_db()
    cur = db.execute(
        "INSERT INTO event_tasks (event_id, title, assignee, created_at, created_by) VALUES (?,?,?,?,?)",
        (event_id, title, assignee, date.today().isoformat(), person)
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@bp.route("/calendar/tasks/<int:task_id>/toggle", methods=["POST"])
def event_tasks_toggle(task_id: int):
    person = auth_person()
    db = get_db()
    row = db.execute("SELECT completed FROM event_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["completed"] else 1
    db.execute(
        "UPDATE event_tasks SET completed=?, completed_at=?, completed_by=? WHERE id=?",
        (new_val, date.today().isoformat() if new_val else None, person if new_val else None, task_id)
    )
    db.commit()
    return jsonify({"ok": True, "completed": new_val})


@bp.route("/calendar/tasks/<int:task_id>/delete", methods=["POST"])
def event_tasks_delete(task_id: int):
    db = get_db()
    db.execute("DELETE FROM event_tasks WHERE id=?", (task_id,))
    db.commit()
    return jsonify({"ok": True})
