"""Calendar blueprint — /calendar, /work_calendar, /calendar/auth, /calendar/oauth2callback, /api/work_meetings."""
from __future__ import annotations

import logging
from datetime import date

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

import config
from modules import calendar_sync
from routes.utils import current_person, get_db, get_prefs

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
    events.sort(key=lambda e: e["start_dt"])

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
    )


@bp.route("/work_calendar", methods=["POST"])
def push_work_calendar():
    """Accept Paul's work meetings pushed as JSON from his work PC."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of meetings"}), 400
    count = calendar_sync.push_work_meetings(data)
    return jsonify({"ok": True, "count": count})


@bp.route("/calendar/auth")
def calendar_auth():
    if current_person() not in config.ADMINS:
        return "Admin only (switch to Katie or Paul first)", 403
    url, code_verifier = calendar_sync.get_auth_url()
    session["oauth_code_verifier"] = code_verifier
    return redirect(url)


@bp.route("/calendar/oauth2callback")
def calendar_oauth_callback():
    code = request.args.get("code")
    if not code:
        return "Missing code", 400
    code_verifier = session.pop("oauth_code_verifier", None)
    ok = calendar_sync.exchange_code(code, get_db(), code_verifier=code_verifier)
    if ok:
        calendar_sync.fetch_events(get_db())
        return redirect(url_for("calendar.calendar_view"))
    return "OAuth failed — check server logs", 500


@bp.route("/api/work_meetings")
def api_work_meetings():
    meetings = calendar_sync.get_work_meetings()
    for m in meetings:
        m["_status"] = calendar_sync.meeting_status(m)
    return jsonify(meetings)
