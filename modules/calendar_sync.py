"""Google Calendar OAuth2 integration + work-meetings cache."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from config import (
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, CALENDAR_ID,
    COLOUR_PERSON, GOOGLE_COLOUR_ID_MAP, CALENDAR_REFRESH_SECS,
    APP_BASE_URL, BEFORE_YOU_LEAVE_RULES,
)

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
REDIRECT_URI = f"{APP_BASE_URL}/calendar/oauth2callback"

# In-memory work meetings state (cleared each new day, like SolarOctopusAPI)
_work_state: dict = {"meetings": [], "date": None}
_work_lock = threading.Lock()


# ── OAuth helpers ─────────────────────────────────────────────────────────────

def _client_config() -> dict:
    return {
        "web": {
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }


def get_auth_url(state: str = "calendar") -> str:
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, state=state)
    flow.redirect_uri = REDIRECT_URI
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return url


def exchange_code(code: str, db_conn) -> bool:
    """Exchange auth code for tokens and persist in DB."""
    try:
        flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
        flow.redirect_uri = REDIRECT_URI
        flow.fetch_token(code=code)
        creds = flow.credentials
        _save_token(db_conn, creds)
        return True
    except Exception as e:
        log.error("OAuth exchange failed: %s", e)
        return False


def _save_token(db_conn, creds: Credentials):
    token_json = creds.to_json()
    db_conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('google_token', ?)",
        (token_json,)
    )
    db_conn.commit()


def _load_token(db_conn) -> Credentials | None:
    row = db_conn.execute(
        "SELECT value FROM app_settings WHERE key='google_token'"
    ).fetchone()
    if not row:
        return None
    try:
        info = json.loads(row["value"])
        return Credentials(
            token=info.get("token"),
            refresh_token=info.get("refresh_token"),
            token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=info.get("client_id", GOOGLE_CLIENT_ID),
            client_secret=info.get("client_secret", GOOGLE_CLIENT_SECRET),
            scopes=info.get("scopes", SCOPES),
        )
    except Exception as e:
        log.warning("Could not load Google token: %s", e)
        return None


def _refresh_if_needed(db_conn, creds: Credentials) -> Credentials:
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(db_conn, creds)
    return creds


# ── Event fetching ────────────────────────────────────────────────────────────

def _colour_to_people(colour_name: str) -> list[str]:
    return COLOUR_PERSON.get(colour_name, COLOUR_PERSON.get("default", []))


def fetch_events(db_conn) -> list[dict]:
    """Fetch upcoming events (next 14 days) from Google Calendar.

    Events that disappear from Google (cancelled/deleted) are kept in the DB
    with cancelled=1 so the family can see what was removed.  New events get
    first_seen_at stamped; existing events preserve their first_seen_at.
    """
    creds = _load_token(db_conn)
    if not creds:
        log.info("No Google Calendar token — skipping fetch")
        return []
    try:
        creds = _refresh_if_needed(db_conn, creds)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=14)
        result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=200,
        ).execute()

        # Build set of IDs Google currently returns
        fetched_ids: set[str] = set()
        upserts = []
        for item in result.get("items", []):
            colour_id   = item.get("colorId", "")
            colour_name = GOOGLE_COLOUR_ID_MAP.get(colour_id, "default")
            attendees   = _colour_to_people(colour_name)
            start   = item.get("start", {})
            end_    = item.get("end", {})
            all_day = "date" in start and "dateTime" not in start
            event_id = item["id"]
            fetched_ids.add(event_id)

            # Preserve first_seen_at if this event already exists
            existing = db_conn.execute(
                "SELECT first_seen_at FROM calendar_events WHERE id=?", (event_id,)
            ).fetchone()
            first_seen = existing["first_seen_at"] if existing else now.isoformat()

            upserts.append({
                "id":           event_id,
                "title":        item.get("summary", "(No title)"),
                "start_dt":     start.get("dateTime", start.get("date", "")),
                "end_dt":       end_.get("dateTime", end_.get("date", "")),
                "colour":       colour_name,
                "all_day":      1 if all_day else 0,
                "attendees":    json.dumps(attendees),
                "cached_at":    now.isoformat(),
                "first_seen_at": first_seen,
                "cancelled":    0,
            })

        # Upsert all currently-live events (restores any that were wrongly cancelled)
        db_conn.executemany(
            """INSERT INTO calendar_events
               (id, title, start_dt, end_dt, colour, all_day, attendees,
                cached_at, first_seen_at, cancelled)
               VALUES (:id,:title,:start_dt,:end_dt,:colour,:all_day,:attendees,
                       :cached_at,:first_seen_at,:cancelled)
               ON CONFLICT(id) DO UPDATE SET
                 title        = excluded.title,
                 start_dt     = excluded.start_dt,
                 end_dt       = excluded.end_dt,
                 colour       = excluded.colour,
                 all_day      = excluded.all_day,
                 attendees    = excluded.attendees,
                 cached_at    = excluded.cached_at,
                 cancelled    = 0""",
            upserts,
        )

        # Soft-cancel events that were in our window but are no longer returned
        # (only cancel future events — past ones age out naturally)
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
                [(now.isoformat(), eid) for eid in to_cancel],
            )
            log.info("Soft-cancelled %d removed event(s)", len(to_cancel))

        db_conn.commit()
        log.info("Synced %d live events (%d cancelled this window)", len(upserts), len(to_cancel))
        return upserts
    except Exception as e:
        log.warning("Calendar fetch error: %s", e)
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
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    events = get_cached_events(db_conn, person)
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
    """Accept meetings pushed from Paul's work PC. Auto-clears on new day."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _work_lock:
        if _work_state["date"] != today:
            _work_state["meetings"] = []
            _work_state["date"] = today
        _work_state["meetings"] = sorted(meetings, key=lambda m: m.get("start", ""))
        return len(_work_state["meetings"])


def get_work_meetings() -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    with _work_lock:
        if _work_state["date"] != today:
            _work_state["meetings"] = []
            _work_state["date"] = today
        return list(_work_state["meetings"])


def meeting_status(meeting: dict) -> str:
    """Return NOW / SOON / LATER / ENDED badge for a work meeting."""
    try:
        now = datetime.now(timezone.utc)
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
