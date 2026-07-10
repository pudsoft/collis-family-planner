"""Google Calendar iCal integration + work-meetings cache."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timezone, timedelta

import requests
from icalendar import Calendar
import recurring_ical_events

from config import (
    COLOUR_PERSON, CALENDAR_REFRESH_SECS, BEFORE_YOU_LEAVE_RULES,
)

log = logging.getLogger(__name__)

_work_state: dict = {"meetings": []}
_work_lock = threading.Lock()

# None = OK / no URL configured; "fetch_error" = last sync failed
_sync_error: str | None = None


def get_sync_error() -> str | None:
    """Return the last sync error type, or None if syncing normally."""
    return _sync_error


# ── iCal helpers ──────────────────────────────────────────────────────────────

def _load_ical_url(db_conn) -> str | None:
    row = db_conn.execute(
        "SELECT value FROM app_settings WHERE key='google_ical_url'"
    ).fetchone()
    return row["value"] if row else None


def _colour_to_people(colour_name: str) -> list[str]:
    return COLOUR_PERSON.get(colour_name, COLOUR_PERSON.get("default", []))


# ── Event fetching ────────────────────────────────────────────────────────────

def fetch_events(db_conn) -> list[dict]:
    """Fetch upcoming events (next 14 days) from Google Calendar via private iCal URL.

    Events that disappear from Google (cancelled/deleted) are kept in the DB
    with cancelled=1 so the family can see what was removed.  New events get
    first_seen_at stamped; existing events preserve their first_seen_at.
    """
    global _sync_error
    url = _load_ical_url(db_conn)
    if not url:
        log.info("No iCal URL configured — skipping fetch")
        return []

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)

        now = datetime.now(timezone.utc)
        end = now + timedelta(days=14)
        now_ts = now.isoformat()

        components = recurring_ical_events.of(cal).between(now, end)

        fetched_ids: set[str] = set()
        upserts = []

        for vevent in components:
            if str(vevent.get("STATUS", "")).upper() == "CANCELLED":
                continue

            dtstart = vevent.get("DTSTART")
            dtend   = vevent.get("DTEND")
            if dtstart is None:
                continue

            start_val = dtstart.dt
            end_val   = dtend.dt if dtend else start_val
            all_day   = isinstance(start_val, date) and not isinstance(start_val, datetime)

            if all_day:
                start_iso = start_val.isoformat()
                end_iso   = end_val.isoformat()
            else:
                if getattr(start_val, "tzinfo", None) is None:
                    start_val = start_val.replace(tzinfo=timezone.utc)
                if getattr(end_val, "tzinfo", None) is None:
                    end_val = end_val.replace(tzinfo=timezone.utc)
                start_iso = start_val.isoformat()
                end_iso   = end_val.isoformat()

            uid = str(vevent.get("UID", ""))
            event_id = f"{uid}_{start_iso}"

            # Google Calendar exports event colours as e.g. COLOR:tomato
            colour_name = str(vevent.get("COLOR", "") or "").lower().strip() or "default"
            attendees   = _colour_to_people(colour_name)
            fetched_ids.add(event_id)

            existing = db_conn.execute(
                "SELECT first_seen_at FROM calendar_events WHERE id=?", (event_id,)
            ).fetchone()
            first_seen = existing["first_seen_at"] if existing else now_ts

            upserts.append((
                event_id,
                str(vevent.get("SUMMARY", "(No title)")),
                start_iso,
                end_iso,
                colour_name,
                1 if all_day else 0,
                json.dumps(attendees),
                now_ts,
                first_seen,
                0,
            ))

        if upserts:
            db_conn.executemany(
                """INSERT OR REPLACE INTO calendar_events
                   (id, title, start_dt, end_dt, colour, all_day, attendees,
                    cached_at, first_seen_at, cancelled)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                upserts,
            )

        # Soft-cancel events that were in our window but are no longer returned
        window_start = now.isoformat()
        window_end   = end.isoformat()
        existing_in_window = db_conn.execute(
            """SELECT id FROM calendar_events
               WHERE cancelled = 0
                 AND start_dt >= ? AND start_dt <= ?""",
            (window_start, window_end),
        ).fetchall()
        to_cancel = [r["id"] for r in existing_in_window if r["id"] not in fetched_ids]
        if to_cancel:
            db_conn.executemany(
                "UPDATE calendar_events SET cancelled=1, cached_at=? WHERE id=?",
                [(now_ts, eid) for eid in to_cancel],
            )
            log.info("Soft-cancelled %d removed event(s)", len(to_cancel))

        db_conn.commit()
        _sync_error = None
        log.info("Synced %d live events (%d cancelled this window)", len(upserts), len(to_cancel))
        return [
            {
                "id": r[0], "title": r[1], "start_dt": r[2], "end_dt": r[3],
                "colour": r[4], "all_day": r[5], "attendees": json.loads(r[6]),
                "cached_at": r[7], "first_seen_at": r[8], "cancelled": r[9],
            }
            for r in upserts
        ]

    except Exception as e:
        _sync_error = "fetch_error"
        log.warning("Calendar iCal fetch error: %s", e)
        return []


def get_cached_events(db_conn, person: str = None,
                      include_cancelled: bool = True) -> list[dict]:
    """Return cached events, optionally filtered to a person.

    Cancelled events are always included (they render faded in the UI) unless
    include_cancelled=False.  They are sorted so live events appear before
    cancelled ones on the same date.
    """
    rows = db_conn.execute(
        "SELECT * FROM calendar_events ORDER BY start_dt, cancelled"
    ).fetchall()
    events = [dict(r) for r in rows]
    for e in events:
        e["attendees"] = json.loads(e.get("attendees") or "[]")
    if not include_cancelled:
        events = [e for e in events if not e.get("cancelled")]
    if person and person != "family":
        events = [e for e in events if person in e["attendees"]]
    return events


def get_today_events(db_conn, person: str = None) -> list[dict]:
    """Today's events for dashboard — excludes cancelled so the strip stays clean."""
    today = datetime.now().strftime("%Y-%m-%d")
    events = get_cached_events(db_conn, person, include_cancelled=False)
    return [e for e in events if e["start_dt"].startswith(today)]


# ── Before-you-leave ──────────────────────────────────────────────────────────

def before_you_leave(db_conn, person: str) -> list[str]:
    """Return checklist items based on today's + tomorrow's events for this person."""
    today    = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    events   = get_cached_events(db_conn, person)
    upcoming = [
        e for e in events
        if e["start_dt"].startswith(today) or e["start_dt"].startswith(tomorrow)
    ]
    suggestions = set()
    for event in upcoming:
        title_lower = event["title"].lower()
        for keywords, item in BEFORE_YOU_LEAVE_RULES:
            if any(kw in title_lower for kw in keywords):
                suggestions.add(item)
    return sorted(suggestions)


# ── Childcare warning ────────────────────────────────────────────────────────

def childcare_warning(db_conn) -> dict | None:
    """Return warning info if Katie is at Toby AND Paul isn't on A/L
    but Joshua or Violet have nothing scheduled between 09:00–17:30."""
    today      = datetime.now().strftime("%Y-%m-%d")
    all_events = get_cached_events(db_conn, include_cancelled=False)
    today_events = [e for e in all_events if e["start_dt"].startswith(today)]

    katie_at_work = any(
        "toby" in e["title"].lower() and "katie" in e["attendees"]
        for e in today_events
    )
    if not katie_at_work:
        return None

    paul_on_leave = any(
        ("a/l" in e["title"].lower() or "annual leave" in e["title"].lower())
        and "paul" in e["attendees"]
        for e in today_events
    )
    if paul_on_leave:
        return None

    window_start = f"{today}T09:00:00"
    window_end   = f"{today}T17:30:00"
    uncovered = []
    for child in ["joshua", "violet"]:
        covered = any(
            child in e["attendees"]
            and not e.get("all_day")
            and e["start_dt"] >= window_start
            and e["start_dt"] <= window_end
            for e in today_events
        )
        if not covered:
            uncovered.append(child)

    if uncovered:
        return {"uncovered": uncovered, "katie_at": "Toby"}
    return None


def first_events_today(db_conn, people: list) -> dict:
    """Return the first timed event today for each person in the list."""
    today      = datetime.now().strftime("%Y-%m-%d")
    all_events = get_cached_events(db_conn, include_cancelled=False)
    timed      = [
        e for e in all_events
        if e["start_dt"].startswith(today) and not e.get("all_day")
    ]
    timed.sort(key=lambda e: e["start_dt"])
    result = {}
    for p in people:
        for e in timed:
            if p in e["attendees"]:
                result[p] = e
                break
    return result


# ── Background refresh thread ─────────────────────────────────────────────────

def start_background_sync(get_db_fn):
    """Start a daemon thread that refreshes calendar every CALENDAR_REFRESH_SECS."""
    def _loop():
        while True:
            try:
                db = get_db_fn()
                fetch_events(db)
                db.close()
            except Exception as e:
                log.warning("Background calendar sync error: %s", e)
            time.sleep(CALENDAR_REFRESH_SECS)

    t = threading.Thread(target=_loop, daemon=True, name="calendar-sync")
    t.start()
    log.info("Calendar background sync started (interval=%ds)", CALENDAR_REFRESH_SECS)


# ── Work meetings (push from work PC) ────────────────────────────────────────

def push_work_meetings(meetings: list[dict]) -> int:
    """Accept meetings pushed from Paul's work PC (up to 7 days ahead)."""
    cutoff = datetime.now().strftime("%Y-%m-%d")
    with _work_lock:
        kept = [m for m in _work_state["meetings"]
                if m.get("start", "")[:10] >= cutoff]
        incoming = sorted(meetings, key=lambda m: m.get("start", ""))
        merged = {(m["start"], m["title"]): m for m in kept}
        for m in incoming:
            merged[(m["start"], m["title"])] = m
        _work_state["meetings"] = sorted(merged.values(), key=lambda m: m.get("start", ""))
        return len(_work_state["meetings"])


def get_work_meetings(date: str = None) -> list[dict]:
    """Return work meetings for a specific date (default: today)."""
    target = date or datetime.now().strftime("%Y-%m-%d")
    with _work_lock:
        return [m for m in _work_state["meetings"]
                if m.get("start", "")[:10] == target]


def get_future_work_meetings() -> list[dict]:
    """Return work meetings for dates strictly after today."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _work_lock:
        return [m for m in _work_state["meetings"]
                if m.get("start", "")[:10] > today]


def meeting_status(meeting: dict) -> str:
    """Return NOW / SOON / LATER / ENDED badge for a work meeting."""
    try:
        now   = datetime.now(timezone.utc)
        start = datetime.fromisoformat(meeting["start"])
        end   = datetime.fromisoformat(meeting["end"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if now >= end:
            return "ENDED"
        if now >= start:
            return "NOW"
        diff = (start - now).total_seconds()
        if diff <= 900:
            return "SOON"
        return "LATER"
    except Exception:
        return "LATER"
