"""Collis Family Planner — main Flask application."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time as _time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

import database

from flask import (
    Flask, g, jsonify, redirect, render_template, request,
    session, url_for,
)

import config
from modules import (
    alexa, auth, calendar_sync, hive, meals, medicines, ntfy, push_notif,
    home_assistant as ha_module, tapo, tasks, unifi, weather,
)

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


# ── Lightweight in-memory page/API response cache ─────────────────────────────
_pcache: dict[str, tuple[float, object]] = {}
_pcache_lock = threading.Lock()


def _pcache_get(key: str, ttl: float):
    """Return cached payload if age < ttl seconds, else None."""
    with _pcache_lock:
        entry = _pcache.get(key)
    if not entry:
        return None
    ts, data = entry
    return data if (_time.time() - ts) < ttl else None


def _pcache_set(key: str, data):
    with _pcache_lock:
        _pcache[key] = (_time.time(), data)


def _pcache_bust(key: str):
    with _pcache_lock:
        _pcache.pop(key, None)


@app.template_filter("fromjson")
def fromjson_filter(s):
    try:
        return json.loads(s) if s else []
    except Exception:
        return []


@app.template_filter("friendlydate")
def friendlydate_filter(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        today = date.today()
        if dt.date() == today:
            return f"Today — {dt.strftime('%A %-d %B')}"
        if dt.date() == today + timedelta(days=1):
            return f"Tomorrow — {dt.strftime('%A %-d %B')}"
        return dt.strftime("%A %-d %B")
    except Exception:
        return date_str

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        if config.DB_DRIVER == "mysql":
            g.db = database.get_connection()
        else:
            db_path = Path(config.DB_PATH)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(str(db_path))
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def _get_db_for_thread():
    """Open a plain connection for background threads (no Flask context)."""
    if config.DB_DRIVER == "mysql":
        return database.get_connection()
    db_path = Path(config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _col_exists_mysql(db, table: str, col: str) -> bool:
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND COLUMN_NAME = ?",
        (table, col),
    ).fetchone()
    return bool(row["cnt"])


def _init_db_mysql(db):
    """Create schema for MySQL HeatWave (OCI production)."""
    statements = [
        """CREATE TABLE IF NOT EXISTS person_prefs (
            person          VARCHAR(50) PRIMARY KEY,
            completed_style VARCHAR(20) NOT NULL DEFAULT 'fade',
            ntfy_channel    TEXT,
            widget_order    TEXT,
            theme           VARCHAR(20) DEFAULT 'default',
            weather_days    INT DEFAULT 3
        )""",
        """CREATE TABLE IF NOT EXISTS app_settings (
            `key`  VARCHAR(100) PRIMARY KEY,
            value  TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS calendar_events (
            id            VARCHAR(200) PRIMARY KEY,
            title         TEXT,
            start_dt      VARCHAR(50),
            end_dt        VARCHAR(50),
            colour        VARCHAR(50),
            all_day       TINYINT DEFAULT 0,
            attendees     TEXT,
            cached_at     VARCHAR(50),
            first_seen_at VARCHAR(50),
            cancelled     TINYINT DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS _migrations (id VARCHAR(100) PRIMARY KEY)""",
        """INSERT IGNORE INTO _migrations VALUES ('calendar_events_v2')""",
        """CREATE TABLE IF NOT EXISTS chore_templates (
            id               INT AUTO_INCREMENT PRIMARY KEY,
            title            VARCHAR(500) NOT NULL,
            interval_days    INT NOT NULL DEFAULT 7,
            default_assignee VARCHAR(50) NOT NULL DEFAULT 'anyone',
            active           TINYINT NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id                     INT AUTO_INCREMENT PRIMARY KEY,
            title                  VARCHAR(500) NOT NULL,
            assignee               VARCHAR(50) NOT NULL DEFAULT 'anyone',
            due_date               VARCHAR(20),
            notes                  TEXT,
            is_chore               TINYINT DEFAULT 0,
            chore_template_id      INT,
            chore_interval_days    INT,
            created_at             VARCHAR(50),
            deferred_to            VARCHAR(20),
            deferred_reason        TEXT,
            completed_by           VARCHAR(50),
            completed_at           VARCHAR(50),
            exec_function_transfer VARCHAR(50),
            FOREIGN KEY (chore_template_id) REFERENCES chore_templates(id)
        )""",
        """CREATE TABLE IF NOT EXISTS meal_plan (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            date        VARCHAR(20) NOT NULL,
            meal_type   VARCHAR(20) NOT NULL,
            recipe_name TEXT,
            servings    INT DEFAULT 4,
            notes       TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS shopping_items (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            item       TEXT NOT NULL,
            quantity   TEXT,
            category   VARCHAR(100) DEFAULT 'Other',
            source     VARCHAR(50) DEFAULT 'manual',
            checked    TINYINT DEFAULT 0,
            week_start VARCHAR(20)
        )""",
        """CREATE TABLE IF NOT EXISTS medicines (
            id                     INT AUTO_INCREMENT PRIMARY KEY,
            name                   VARCHAR(200) NOT NULL,
            person                 VARCHAR(50) NOT NULL,
            daily_dose             DOUBLE DEFAULT 1,
            stock_count            DOUBLE DEFAULT 0,
            reorder_threshold_days INT DEFAULT 14,
            last_ordered           VARCHAR(20),
            notes                  TEXT,
            scheduled_time         VARCHAR(10),
            doses_per_day          INT DEFAULT 1,
            dose_times             TEXT,
            active                 TINYINT DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS medicine_doses (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            medicine_id INT NOT NULL,
            taken_by    VARCHAR(50),
            taken_at    VARCHAR(50),
            dose_date   VARCHAR(20) NOT NULL,
            dose_number INT DEFAULT 1,
            FOREIGN KEY (medicine_id) REFERENCES medicines(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS push_subscriptions (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            person     VARCHAR(50) NOT NULL,
            endpoint   TEXT NOT NULL,
            p256dh     TEXT NOT NULL,
            auth       TEXT NOT NULL,
            created_at VARCHAR(50) DEFAULT NULL,
            UNIQUE KEY uq_endpoint (endpoint(500))
        )""",
        """CREATE TABLE IF NOT EXISTS known_devices (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            display_name VARCHAR(200) NOT NULL,
            mac          VARCHAR(17) NOT NULL UNIQUE,
            person       VARCHAR(50),
            notes        TEXT,
            protected    TINYINT DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS scheduled_reminders (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            title       VARCHAR(200) NOT NULL,
            message     TEXT NOT NULL,
            recipients  TEXT NOT NULL,
            cron_expr   VARCHAR(100) NOT NULL,
            active      TINYINT DEFAULT 1,
            last_sent   VARCHAR(50)
        )""",
        """CREATE TABLE IF NOT EXISTS prn_log (
            id        INT AUTO_INCREMENT PRIMARY KEY,
            person    VARCHAR(50) NOT NULL,
            type      VARCHAR(50) NOT NULL,
            value     DOUBLE,
            logged_at VARCHAR(50) NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS smart_rooms (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            name          VARCHAR(200) NOT NULL,
            icon          VARCHAR(20) DEFAULT '🏠',
            floor         VARCHAR(20) DEFAULT 'ground',
            sort_order    INT DEFAULT 0,
            grid_col      INT DEFAULT 0,
            grid_row      INT DEFAULT 0,
            grid_col_span INT DEFAULT 1,
            grid_row_span INT DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS smart_devices (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            provider    VARCHAR(50) NOT NULL,
            device_id   VARCHAR(200) NOT NULL,
            name        VARCHAR(200) NOT NULL,
            device_type VARCHAR(100),
            room_id     INT,
            FOREIGN KEY (room_id) REFERENCES smart_rooms(id) ON DELETE SET NULL,
            UNIQUE KEY uq_prov_dev (provider, device_id(191))
        )""",
    ]
    for stmt in statements:
        db.execute(stmt)
    db.commit()

    for person in config.PEOPLE:
        db.execute("INSERT IGNORE INTO person_prefs (person) VALUES (?)", (person,))
    db.commit()

    tasks.seed_default_chores(db)

    # Column migrations (safe for repeated runs)
    for table, col, defn in [
        ("person_prefs",   "weather_days",   "INT DEFAULT 3"),
        ("medicines",      "scheduled_time", "VARCHAR(10)"),
        ("known_devices",  "protected",      "TINYINT DEFAULT 0"),
        ("person_prefs",   "theme",          "VARCHAR(20) DEFAULT 'default'"),
        ("person_prefs",   "login_pin",      "VARCHAR(200)"),
        ("medicines",      "doses_per_day",  "INT DEFAULT 1"),
        ("medicines",      "dose_times",     "TEXT"),
        ("medicines",      "active",         "TINYINT DEFAULT 1"),
        ("medicine_doses", "dose_number",    "INT DEFAULT 1"),
        ("person_prefs",     "notif_method",   "VARCHAR(20) DEFAULT 'ntfy'"),
        ("chore_templates",  "repeat_days",    "TEXT"),
        ("person_prefs",     "presence_mac",   "VARCHAR(50)"),
        ("smart_rooms",      "floor",          "VARCHAR(20) DEFAULT 'ground'"),
        ("smart_rooms",      "zone_color",     "VARCHAR(7)"),
        ("smart_devices",    "ha_entity_id",   "VARCHAR(200)"),
    ]:
        if not _col_exists_mysql(db, table, col):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            db.commit()

    db.execute(
        "UPDATE person_prefs SET theme='dark' WHERE person='paul' AND (theme IS NULL OR theme='default')"
    )
    for person, ch in [("paul", config.NTFY_CHANNEL_PAUL), ("katie", config.NTFY_CHANNEL_KATIE)]:
        if ch:
            db.execute(
                "UPDATE person_prefs SET ntfy_channel=? WHERE person=? AND ntfy_channel IS NULL",
                (ch, person),
            )
    db.commit()


def _init_db_sqlite(db):
    """Create schema for SQLite (local dev)."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS person_prefs (
            person          TEXT PRIMARY KEY,
            completed_style TEXT NOT NULL DEFAULT 'fade',
            ntfy_channel    TEXT,
            widget_order    TEXT,
            theme           TEXT DEFAULT 'default',
            weather_days    INTEGER DEFAULT 3
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS calendar_events (
            id            TEXT PRIMARY KEY,
            title         TEXT,
            start_dt      TEXT,
            end_dt        TEXT,
            colour        TEXT,
            all_day       INTEGER DEFAULT 0,
            attendees     TEXT,
            cached_at     TEXT,
            first_seen_at TEXT,
            cancelled     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS _migrations (id TEXT PRIMARY KEY);
        INSERT OR IGNORE INTO _migrations VALUES ('calendar_events_v2');
        CREATE TABLE IF NOT EXISTS chore_templates (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            title            TEXT NOT NULL,
            interval_days    INTEGER NOT NULL DEFAULT 7,
            default_assignee TEXT NOT NULL DEFAULT 'anyone',
            active           INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            title                  TEXT NOT NULL,
            assignee               TEXT NOT NULL DEFAULT 'anyone',
            due_date               TEXT,
            notes                  TEXT,
            is_chore               INTEGER DEFAULT 0,
            chore_template_id      INTEGER REFERENCES chore_templates(id),
            chore_interval_days    INTEGER,
            created_at             TEXT,
            deferred_to            TEXT,
            deferred_reason        TEXT,
            completed_by           TEXT,
            completed_at           TEXT,
            exec_function_transfer TEXT
        );
        CREATE TABLE IF NOT EXISTS meal_plan (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            meal_type   TEXT NOT NULL,
            recipe_name TEXT,
            servings    INTEGER DEFAULT 4,
            notes       TEXT
        );
        CREATE TABLE IF NOT EXISTS shopping_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item       TEXT NOT NULL,
            quantity   TEXT,
            category   TEXT DEFAULT 'Other',
            source     TEXT DEFAULT 'manual',
            checked    INTEGER DEFAULT 0,
            week_start TEXT
        );
        CREATE TABLE IF NOT EXISTS medicines (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            name                   TEXT NOT NULL,
            person                 TEXT NOT NULL,
            daily_dose             REAL DEFAULT 1,
            stock_count            REAL DEFAULT 0,
            reorder_threshold_days INTEGER DEFAULT 14,
            last_ordered           TEXT,
            notes                  TEXT,
            scheduled_time         TEXT,
            doses_per_day          INTEGER DEFAULT 1,
            dose_times             TEXT,
            active                 INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS medicine_doses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
            taken_by    TEXT,
            taken_at    TEXT,
            dose_date   TEXT NOT NULL,
            dose_number INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            person     TEXT NOT NULL,
            endpoint   TEXT NOT NULL UNIQUE,
            p256dh     TEXT NOT NULL,
            auth       TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS known_devices (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL,
            mac          TEXT NOT NULL UNIQUE,
            person       TEXT,
            notes        TEXT,
            protected    INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS scheduled_reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            message     TEXT NOT NULL,
            recipients  TEXT NOT NULL,
            cron_expr   TEXT NOT NULL,
            active      INTEGER DEFAULT 1,
            last_sent   TEXT
        );
        CREATE TABLE IF NOT EXISTS prn_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            person    TEXT NOT NULL,
            type      TEXT NOT NULL,
            value     REAL,
            logged_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS smart_rooms (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            icon          TEXT DEFAULT '🏠',
            floor         TEXT DEFAULT 'ground',
            sort_order    INTEGER DEFAULT 0,
            grid_col      INTEGER DEFAULT 0,
            grid_row      INTEGER DEFAULT 0,
            grid_col_span INTEGER DEFAULT 1,
            grid_row_span INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS smart_devices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            provider    TEXT NOT NULL,
            device_id   TEXT NOT NULL,
            name        TEXT NOT NULL,
            device_type TEXT,
            room_id     INTEGER REFERENCES smart_rooms(id),
            UNIQUE(provider, device_id)
        );
    """)
    db.commit()

    for person in config.PEOPLE:
        db.execute("INSERT OR IGNORE INTO person_prefs (person) VALUES (?)", (person,))
    db.commit()

    tasks.seed_default_chores(db)

    for table, col, pragma_type in [
        ("person_prefs",   "weather_days",   "INTEGER DEFAULT 3"),
        ("medicines",      "scheduled_time", "TEXT"),
        ("known_devices",  "protected",      "INTEGER DEFAULT 0"),
        ("person_prefs",   "theme",          "TEXT DEFAULT 'default'"),
        ("person_prefs",   "login_pin",      "TEXT"),
        ("medicines",      "doses_per_day",  "INTEGER DEFAULT 1"),
        ("medicines",      "dose_times",     "TEXT"),
        ("medicines",      "active",         "INTEGER DEFAULT 1"),
        ("medicine_doses", "dose_number",    "INTEGER DEFAULT 1"),
        ("person_prefs",     "notif_method",  "TEXT DEFAULT 'ntfy'"),
        ("chore_templates",  "repeat_days",   "TEXT"),
        ("person_prefs",     "presence_mac",  "TEXT"),
        ("smart_rooms",      "floor",         "TEXT DEFAULT 'ground'"),
        ("smart_rooms",      "zone_color",    "TEXT"),
        ("smart_devices",    "ha_entity_id",  "TEXT"),
    ]:
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {pragma_type}")
            db.commit()

    db.execute(
        "UPDATE person_prefs SET theme='dark' WHERE person='paul' AND (theme IS NULL OR theme='default')"
    )
    for person, ch in [("paul", config.NTFY_CHANNEL_PAUL), ("katie", config.NTFY_CHANNEL_KATIE)]:
        if ch:
            db.execute(
                "UPDATE person_prefs SET ntfy_channel=? WHERE person=? AND ntfy_channel IS NULL",
                (ch, person),
            )
    db.commit()


def init_db():
    with app.app_context():
        db = get_db()
        if config.DB_DRIVER == "mysql":
            _init_db_mysql(db)
            log.info("Database initialised (MySQL: %s/%s)", config.MYSQL_HOST, config.MYSQL_DB)
        else:
            _init_db_sqlite(db)
            log.info("Database initialised (SQLite: %s)", config.DB_PATH)


# ── Helpers ───────────────────────────────────────────────────────────────────

_LOGIN_EXEMPT = {
    "login", "login_pin", "login_google", "login_google_callback",
    "logout", "static", "service_worker", "offline",
}

@app.before_request
def check_auth_and_person():
    if request.endpoint in _LOGIN_EXEMPT:
        return
    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.url))
    p = request.args.get("person")
    if p and p in config.PEOPLE + ["family"]:
        session["person"] = p


def current_person() -> str:
    return session.get("person", "family")


def auth_person() -> str:
    """The person who actually logged in — never changes with the view switcher."""
    return session.get("auth_person") or session.get("person", "family")


def get_prefs(db, person: str) -> dict:
    row = db.execute("SELECT * FROM person_prefs WHERE person=?", (person,)).fetchone()
    return dict(row) if row else {"completed_style": "fade"}


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        if current_person() not in config.ADMINS:
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated


# ── Login / logout ────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))
    next_url = request.args.get("next", "")
    return render_template(
        "login.html",
        people=config.PEOPLE + ["family"],
        person_display=config.PERSON_DISPLAY,
        google_persons=list(auth.GOOGLE_LOGIN_PERSONS),
        google_enabled=bool(config.GOOGLE_CLIENT_ID),
        next=next_url,
        error=request.args.get("error", ""),
    )


@app.route("/login/pin", methods=["POST"])
def login_pin():
    person  = request.form.get("person", "").strip()
    pin_val = request.form.get("pin", "").strip()
    next_url = request.form.get("next", "") or url_for("dashboard")

    if person not in config.PEOPLE + ["family"]:
        return redirect(url_for("login", error="Invalid person", next=next_url))

    if person in auth.GOOGLE_LOGIN_PERSONS:
        return redirect(url_for("login", error="Please use Google to sign in", next=next_url))

    db = get_db()

    if person == "family":
        row = db.execute("SELECT value FROM app_settings WHERE key='family_passcode'").fetchone()
        hashed = row["value"] if row else None
    else:
        row = db.execute("SELECT login_pin FROM person_prefs WHERE person=?", (person,)).fetchone()
        hashed = row["login_pin"] if row else None

    if not hashed or not auth.check_pin(pin_val, hashed):
        return redirect(url_for("login", error="Incorrect PIN", next=next_url))

    session.permanent = True
    session["authenticated"] = True
    session["person"]      = person
    session["auth_person"] = person
    return redirect(next_url)


def _google_redirect_uri() -> str:
    return config.APP_BASE_URL.rstrip("/") + "/login/google/callback"


@app.route("/login/google")
def login_google():
    url, state = auth.google_login_url(_google_redirect_uri())
    session["oauth_login_state"] = state
    return redirect(url)


@app.route("/login/google/callback")
def login_google_callback():
    if request.args.get("state") != session.pop("oauth_login_state", None):
        return redirect(url_for("login", error="Invalid state — please try again"))
    code = request.args.get("code")
    if not code:
        return redirect(url_for("login", error="Google login cancelled"))
    email = auth.google_exchange_code(code, _google_redirect_uri())
    if not email:
        return redirect(url_for("login", error="Could not retrieve email from Google"))
    person = config.GOOGLE_AUTHORIZED_EMAILS.get(email)
    if not person:
        return redirect(url_for("login", error=f"Email not authorised: {email}"))
    session.permanent = True
    session["authenticated"] = True
    session["person"]      = person
    session["auth_person"] = person
    return redirect(url_for("dashboard"))


@app.route("/sw.js")
def service_worker():
    return app.send_static_file("sw.js"), 200, {"Content-Type": "application/javascript"}


@app.route("/offline")
def offline():
    return render_template("offline.html"), 200


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _week_days(week_start: date = None) -> list[str]:
    start = week_start or meals.get_week_start()
    return [(start + timedelta(days=i)).isoformat() for i in range(7)]


# ── Person switching ──────────────────────────────────────────────────────────

@app.route("/set_person/<person>")
def set_person(person: str):
    if person in config.PEOPLE + ["family"]:
        session["person"] = person
    # Redirect back but strip ?person= so the route doesn't override the session
    referrer = request.referrer or url_for("dashboard")
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    parsed = urlparse(referrer)
    qs = {k: v for k, v in parse_qs(parsed.query).items() if k != "person"}
    clean = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    return redirect(clean)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/dashboard")
def dashboard():
    person = current_person()
    # Override person from URL param (NTFY deep-link)
    if "person" in request.args and request.args["person"] in config.PEOPLE + ["family"]:
        person = request.args["person"]
        session["person"] = person

    db   = get_db()
    prefs = get_prefs(db, person)

    today_events       = calendar_sync.get_today_events(db, person)
    work_meetings      = calendar_sync.get_work_meetings() if person in ("paul", "family") else []
    leave_checklist    = calendar_sync.before_you_leave(db, person)
    today_tasks        = tasks.get_tasks_for_person(db, person)
    today_meds         = medicines.get_today_doses(db, person)
    wx                 = weather.get_weather()
    childcare_alert    = calendar_sync.childcare_warning(db)
    kids_first_events  = calendar_sync.first_events_today(db, ["joshua", "violet"]) if person in ("paul", "family") else {}
    weather_days       = int(prefs.get("weather_days") or 3)

    # Non-admins only see their own medicines (same rule as /medicines page)
    viewer = auth_person()
    if viewer not in config.ADMINS:
        today_meds = [m for m in today_meds if m["person"] == viewer]

    return render_template(
        "dashboard.html",
        person=person,
        prefs=prefs,
        today_events=today_events,
        work_meetings=work_meetings,
        leave_checklist=leave_checklist,
        today_tasks=today_tasks,
        today_meds=today_meds,
        weather=wx,
        childcare_alert=childcare_alert,
        kids_first_events=kids_first_events,
        weather_days=weather_days,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        today=date.today().isoformat(),
        is_admin=person in config.ADMINS,
        calendar_error=calendar_sync.get_sync_error() if person in config.ADMINS else None,
    )


# ── Calendar ──────────────────────────────────────────────────────────────────

@app.route("/calendar")
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


@app.route("/work_calendar", methods=["POST"])
def push_work_calendar():
    """Accept Paul's work meetings pushed as JSON from his work PC."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of meetings"}), 400
    count = calendar_sync.push_work_meetings(data)
    return jsonify({"ok": True, "count": count})


@app.route("/calendar/auth")
def calendar_auth():
    if current_person() not in config.ADMINS:
        return "Admin only (switch to Katie or Paul first)", 403
    url, code_verifier = calendar_sync.get_auth_url()
    session["oauth_code_verifier"] = code_verifier
    return redirect(url)


@app.route("/calendar/oauth2callback")
def calendar_oauth_callback():
    code = request.args.get("code")
    if not code:
        return "Missing code", 400
    code_verifier = session.pop("oauth_code_verifier", None)
    ok = calendar_sync.exchange_code(code, get_db(), code_verifier=code_verifier)
    if ok:
        calendar_sync.fetch_events(get_db())
        return redirect(url_for("calendar_view"))
    return "OAuth failed — check server logs", 500


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.route("/tasks")
def tasks_view():
    person = current_person()
    if "person" in request.args and request.args["person"] in config.PEOPLE + ["family"]:
        person = request.args["person"]
        session["person"] = person

    highlight_task = request.args.get("task", type=int)
    show_done      = request.args.get("show_done", "0") == "1"
    db             = get_db()
    prefs          = get_prefs(db, person)
    task_list      = tasks.get_tasks_for_person(db, person, include_done=show_done)

    return render_template(
        "tasks.html",
        person=person,
        prefs=prefs,
        task_list=task_list,
        highlight_task=highlight_task,
        show_done=show_done,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        today=date.today().isoformat(),
        is_admin=person in config.ADMINS,
    )


@app.route("/tasks/create", methods=["POST"])
def task_create():
    d = request.form
    tasks.create_task(
        get_db(),
        title    = d.get("title", "").strip(),
        assignee = d.get("assignee", "anyone"),
        due_date = d.get("due_date") or None,
        notes    = d.get("notes") or None,
    )
    return redirect(url_for("tasks_view"))


@app.route("/tasks/<int:task_id>/complete", methods=["POST"])
def task_complete(task_id: int):
    person = current_person()
    tasks.complete_task(get_db(), task_id, person)
    return jsonify({"ok": True})


@app.route("/tasks/<int:task_id>/uncomplete", methods=["POST"])
def task_uncomplete(task_id: int):
    tasks.uncomplete_task(get_db(), task_id)
    return jsonify({"ok": True})


@app.route("/tasks/<int:task_id>/defer", methods=["POST"])
def task_defer(task_id: int):
    defer_to = request.form.get("defer_to") or (date.today() + timedelta(days=1)).isoformat()
    reason   = request.form.get("reason")
    tasks.defer_task(get_db(), task_id, defer_to, reason)
    return jsonify({"ok": True})


@app.route("/tasks/<int:task_id>/transfer", methods=["POST"])
def task_transfer(task_id: int):
    person    = current_person()
    if person not in config.ADMINS:
        return jsonify({"error": "Admins only"}), 403
    recipient = tasks.transfer_exec_function(get_db(), task_id, person)

    # Send NTFY to recipient
    db    = get_db()
    prefs = get_prefs(db, recipient)
    ch    = prefs.get("ntfy_channel")
    if ch:
        task_row = db.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task_row:
            ntfy.send_task_reminder(ch, recipient, task_id, task_row["title"], priority="high")

    return jsonify({"ok": True, "transferred_to": recipient})


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def task_delete(task_id: int):
    tasks.delete_task(get_db(), task_id)
    return jsonify({"ok": True})


# ── Meals ─────────────────────────────────────────────────────────────────────

@app.route("/meals")
def meals_view():
    person     = current_person()
    db         = get_db()
    prefs      = get_prefs(db, person)
    week_start = request.args.get("week") or meals.get_week_start().isoformat()
    week_days  = _week_days(date.fromisoformat(week_start))
    plan       = meals.get_meal_plan(db, week_start)
    shopping   = meals.get_shopping_list(db)

    prev_week = (date.fromisoformat(week_start) - timedelta(days=7)).isoformat()
    next_week = (date.fromisoformat(week_start) + timedelta(days=7)).isoformat()

    # Weekdays: evening only; weekends: lunch + evening
    day_meal_types = {
        d: (["Lunch", "Dinner"] if date.fromisoformat(d).weekday() >= 5 else ["Dinner"])
        for d in week_days
    }

    return render_template(
        "meals.html",
        person=person,
        prefs=prefs,
        week_start=week_start,
        week_days=week_days,
        plan=plan,
        shopping=shopping,
        prev_week=prev_week,
        next_week=next_week,
        day_meal_types=day_meal_types,
        categories=meals.ASDA_CATEGORIES,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
        alexa_connected=alexa.is_connected(db),
    )


@app.route("/meals/set", methods=["POST"])
def meal_set():
    d = request.form
    meals.set_meal(get_db(), d["date"], d["meal_type"], d["recipe_name"],
                   int(d.get("servings", 4)), d.get("notes"))
    return jsonify({"ok": True})


@app.route("/meals/clear", methods=["POST"])
def meal_clear():
    d = request.form
    meals.clear_meal(get_db(), d["date"], d["meal_type"])
    return jsonify({"ok": True})


@app.route("/shopping/add", methods=["POST"])
def shopping_add():
    d = request.form
    item_id = meals.add_shopping_item(
        get_db(), d["item"], d.get("quantity"), d.get("category", "Other"), "manual",
    )
    return jsonify({"ok": True, "id": item_id})


@app.route("/shopping/<int:item_id>/check", methods=["POST"])
def shopping_check(item_id: int):
    checked = request.form.get("checked", "1") == "1"
    meals.check_shopping_item(get_db(), item_id, checked)
    return jsonify({"ok": True})


@app.route("/shopping/<int:item_id>/delete", methods=["POST"])
def shopping_delete(item_id: int):
    meals.delete_shopping_item(get_db(), item_id)
    return jsonify({"ok": True})


@app.route("/shopping/clear_checked", methods=["POST"])
def shopping_clear_checked():
    meals.clear_checked_items(get_db())
    return jsonify({"ok": True})


# ── Alexa Shopping List ───────────────────────────────────────────────────────

@app.route("/alexa/auth")
def alexa_auth():
    if "person" in request.args:
        session["person"] = request.args["person"]
    if current_person() not in config.ADMINS:
        return "Admin only — add ?person=paul or ?person=katie to the URL", 403
    if not config.ALEXA_CLIENT_ID:
        return "ALEXA_CLIENT_ID not set in .env — see .env.example", 400
    return redirect(alexa.get_auth_url())


@app.route("/alexa/oauth2callback")
def alexa_oauth_callback():
    code = request.args.get("code")
    if not code:
        error = request.args.get("error", "unknown")
        log.warning("Alexa OAuth callback error: %s", error)
        return f"Alexa auth failed: {error}", 400
    ok = alexa.exchange_code(code, get_db())
    if ok:
        return redirect(url_for("meals_view"))
    return "Alexa auth failed — check server logs", 500


@app.route("/shopping/sync_alexa", methods=["POST"])
@require_admin
def shopping_sync_alexa():
    if not config.ALEXA_CLIENT_ID:
        return jsonify({"error": "Alexa not configured"}), 400
    pushed = alexa.sync_shopping_list_to_alexa(get_db())
    return jsonify({"ok": True, "pushed": pushed})


@app.route("/shopping/alexa_items")
def shopping_alexa_items():
    if not alexa.is_connected(get_db()):
        return jsonify([])
    return jsonify(alexa.get_alexa_shopping_items(get_db()))


# ── Medicines ─────────────────────────────────────────────────────────────────

@app.route("/medicines")
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
    all_meds.sort(key=lambda m: (0 if m["person"] == person else 1, m["person"], m["name"]))

    # Non-admins (Joshua, Violet) only ever see their own medicines,
    # regardless of which view (family, etc.) they're currently in.
    viewer       = auth_person()
    viewer_admin = viewer in config.ADMINS
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
    )


@app.route("/medicines/<int:med_id>/take", methods=["POST"])
def medicine_take(med_id: int):
    person      = current_person()
    dose_date   = request.form.get("dose_date") or None
    dose_number = int(request.form.get("dose_number") or 1)
    taken = medicines.log_dose(get_db(), med_id, person, dose_date=dose_date, dose_number=dose_number)
    return jsonify({"ok": True, "already_taken": not taken})


@app.route("/medicines/doses_for_date")
def medicines_doses_for_date():
    d = request.args.get("date", "").strip()
    if not d:
        return jsonify([])
    meds = medicines.get_doses_for_date(get_db(), d)
    return jsonify(meds)


@app.route("/medicines/<int:med_id>/untake", methods=["POST"])
def medicine_untake(med_id: int):
    dose_date   = request.form.get("dose_date") or None
    dose_number = int(request.form.get("dose_number") or 1)
    medicines.unlog_dose(get_db(), med_id, dose_date=dose_date, dose_number=dose_number)
    return jsonify({"ok": True})


@app.route("/medicines/<int:med_id>/reordered", methods=["POST"])
def medicine_reordered(med_id: int):
    if auth_person() not in config.ADMINS:
        return jsonify({"error": "Admin only"}), 403
    new_stock = request.form.get("new_stock", type=float)
    medicines.mark_reordered(get_db(), med_id, new_stock)
    return jsonify({"ok": True})


@app.route("/prn/log", methods=["POST"])
def prn_log():
    person   = request.form.get("person") or current_person()
    prn_type = request.form.get("type")
    value    = request.form.get("value", type=float)
    if prn_type not in ("paracetamol", "ibuprofen", "temperature"):
        return jsonify({"ok": False, "error": "Invalid type"}), 400
    medicines.log_prn(get_db(), person, prn_type, value)
    return jsonify({"ok": True})


@app.route("/prn/recent")
def prn_recent():
    person = request.args.get("person") or current_person()
    rows   = medicines.get_prn_log(get_db(), person)
    return jsonify(rows)


# ── Settings (personal) ───────────────────────────────────────────────────────

@app.route("/settings")
def settings_view():
    person = auth_person()
    db     = get_db()
    prefs  = get_prefs(db, person)

    google_connected = bool(
        db.execute("SELECT value FROM app_settings WHERE key='google_token'").fetchone()
    )
    return render_template(
        "settings.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
        google_connected=google_connected,
        vapid_public_key=push_notif.get_public_key(db),
    )


@app.route("/settings/save", methods=["POST"])
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
    return redirect(url_for("settings_view"))


@app.route("/settings/change_pin", methods=["POST"])
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


@app.route("/settings/ntfy_test", methods=["POST"])
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


# ── Web Push ──────────────────────────────────────────────────────────────────

@app.route("/push/vapid-public-key")
def push_vapid_public_key():
    return jsonify({"key": push_notif.get_public_key(get_db())})


@app.route("/push/subscribe", methods=["POST"])
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


@app.route("/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    data     = request.get_json(force=True)
    endpoint = data.get("endpoint", "").strip()
    if endpoint:
        db = get_db()
        db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        db.commit()
    return jsonify({"ok": True})


@app.route("/push/test", methods=["POST"])
def push_test():
    person = auth_person()
    sent   = push_notif.send_push_to_person(
        get_db(), person,
        title="✅ Push Test",
        body="Family Planner push notifications are working!",
        url=f"{config.APP_BASE_URL}/dashboard?person={person}",
    )
    return jsonify({"ok": True, "sent": sent})


# ── Network (WiFi + devices) ──────────────────────────────────────────────────

@app.route("/network")
def network_view():
    person = current_person()
    if person not in config.ADMINS:
        return redirect(url_for("settings_view"))
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


@app.route("/network/status")
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
    clients = unifi.list_connected_clients()
    connected_mac_set = {c["mac"].lower() for c in clients}
    # Presence: which tracked people are home
    presence_rows = db.execute(
        "SELECT person, presence_mac FROM person_prefs WHERE presence_mac IS NOT NULL AND presence_mac != ''"
    ).fetchall()
    presence = {
        r["person"]: r["presence_mac"].lower() in connected_mac_set
        for r in presence_rows
    }
    _out = {
        "wlans":          unifi.list_wlans(),
        "clients":        clients,
        "blocked_macs":   list(unifi.list_blocked_macs()),
        "protected_macs": protected_macs,
        "presence":       presence,
    }
    _pcache_set("network_status", _out)
    return jsonify(_out)


@app.route("/admin/presence_mac", methods=["POST"])
@require_admin
def admin_save_presence_mac():
    db = get_db()
    for person in config.PEOPLE:
        mac = request.form.get(f"mac_{person}", "").strip().lower()
        db.execute("INSERT OR IGNORE INTO person_prefs (person) VALUES (?)", (person,))
        db.execute("UPDATE person_prefs SET presence_mac=? WHERE person=?",
                   (mac if mac else None, person))
    db.commit()
    return jsonify({"ok": True})


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_view():
    person = current_person()
    if person not in config.ADMINS:
        return redirect(url_for("settings_view"))
    db = get_db()
    prefs        = get_prefs(db, person)
    chore_templates = [dict(r) for r in db.execute("SELECT * FROM chore_templates ORDER BY title").fetchall()]
    all_meds     = sorted(medicines.get_medicines(db), key=lambda m: m["name"].lower())
    all_devices  = [dict(r) for r in db.execute("SELECT * FROM known_devices ORDER BY display_name").fetchall()]
    google_connected = bool(
        db.execute("SELECT value FROM app_settings WHERE key='google_token'").fetchone()
    )
    live_clients = unifi.list_connected_clients()

    pin_rows = db.execute(
        "SELECT person, login_pin FROM person_prefs WHERE person IN (?,?,?)",
        ("joshua", "violet", "family"),
    ).fetchall()
    family_passcode_row = db.execute(
        "SELECT value FROM app_settings WHERE key='family_passcode'"
    ).fetchone()
    pin_status = {r["person"]: bool(r["login_pin"]) for r in pin_rows}
    pin_status["family"] = bool(family_passcode_row and family_passcode_row["value"])

    presence_rows = db.execute(
        "SELECT person, presence_mac FROM person_prefs WHERE person IN ({})".format(
            ",".join("?" * len(config.PEOPLE))
        ),
        config.PEOPLE,
    ).fetchall()
    presence_macs = {r["person"]: r["presence_mac"] or "" for r in presence_rows}

    return render_template(
        "admin.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=True,
        all_devices=all_devices,
        chore_templates=chore_templates,
        all_meds=all_meds,
        google_connected=google_connected,
        admin_pin=config.ADMIN_PIN,
        live_clients=live_clients,
        pin_status=pin_status,
        presence_macs=presence_macs,
    )


@app.route("/admin/chore", methods=["POST"])
@require_admin
def admin_chore_save():
    d  = request.form
    db = get_db()
    chore_id = d.get("id", type=int)

    # repeat_days: JSON list of weekday ints (0=Mon..6=Sun), or None for interval mode
    repeat_days_raw = d.get("repeat_days", "").strip()
    repeat_days     = repeat_days_raw if repeat_days_raw else None
    # interval_days still stored (used as fallback / display); default 7
    interval_days   = int(d.get("interval_days") or 7)

    if chore_id:
        db.execute(
            """UPDATE chore_templates
               SET title=?, interval_days=?, default_assignee=?, active=?, repeat_days=?
               WHERE id=?""",
            (d["title"], interval_days, d["assignee"], int(d.get("active", 1)),
             repeat_days, chore_id),
        )
    else:
        db.execute(
            """INSERT INTO chore_templates (title, interval_days, default_assignee, repeat_days)
               VALUES (?,?,?,?)""",
            (d["title"], interval_days, d.get("assignee", "anyone"), repeat_days),
        )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/chore/<int:chore_id>/delete", methods=["POST"])
@require_admin
def admin_chore_delete(chore_id: int):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE chore_template_id=?", (chore_id,))
    db.execute("DELETE FROM chore_templates WHERE id=?", (chore_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/set_pin", methods=["POST"])
@require_admin
def admin_set_pin():
    person  = request.form.get("person", "").strip()
    pin_val = request.form.get("pin", "").strip()
    clear   = request.form.get("clear") == "1"
    db      = get_db()

    valid_targets = list(config.PEOPLE) + ["family"]
    if person not in valid_targets:
        return jsonify({"error": "Unknown person"}), 400

    if clear:
        if person == "family":
            db.execute("DELETE FROM app_settings WHERE key='family_passcode'")
        else:
            db.execute("UPDATE person_prefs SET login_pin=NULL WHERE person=?", (person,))
        db.commit()
        return jsonify({"ok": True})

    if len(pin_val) < 4:
        return jsonify({"error": "PIN must be at least 4 digits"}), 400

    hashed = auth.hash_pin(pin_val)
    if person == "family":
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('family_passcode', ?)",
            (hashed,),
        )
    else:
        db.execute("UPDATE person_prefs SET login_pin=? WHERE person=?", (hashed, person))
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/medicine", methods=["POST"])
@require_admin
def admin_medicine_save():
    d             = request.form
    db            = get_db()
    med_id        = d.get("id", type=int)
    doses_per_day = max(1, int(d.get("doses_per_day") or 1))
    active        = 1 if d.get("active") != "0" else 0

    # Build dose_times JSON from individual time fields
    raw_times = [d.get(f"dose_time_{i}", "").strip() for i in range(1, doses_per_day + 1)]
    dose_times = json.dumps([t or None for t in raw_times]) if doses_per_day > 1 else None
    # For single dose keep scheduled_time for legacy compat
    scheduled_time = raw_times[0] if raw_times[0] else None

    kwargs = dict(
        name=d["name"], person=d["person"],
        daily_dose=float(d.get("daily_dose", 1)),
        stock_count=float(d.get("stock_count", 0)),
        reorder_threshold_days=int(d.get("reorder_threshold_days", 14)),
        notes=d.get("notes") or None,
        scheduled_time=scheduled_time,
        doses_per_day=doses_per_day,
        dose_times=dose_times,
        active=active,
    )
    if med_id:
        medicines.update_medicine(db, med_id, **kwargs)
    else:
        medicines.add_medicine(db, **kwargs)
    return jsonify({"ok": True})


@app.route("/admin/medicine/<int:med_id>/toggle_active", methods=["POST"])
@require_admin
def admin_medicine_toggle_active(med_id: int):
    db  = get_db()
    row = db.execute("SELECT active FROM medicines WHERE id=?", (med_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["active"] else 1
    db.execute("UPDATE medicines SET active=? WHERE id=?", (new_val, med_id))
    db.commit()
    return jsonify({"ok": True, "active": new_val})


@app.route("/admin/medicine/<int:med_id>/delete", methods=["POST"])
@require_admin
def admin_medicine_delete(med_id: int):
    medicines.delete_medicine(get_db(), med_id)
    return jsonify({"ok": True})


@app.route("/admin/device", methods=["POST"])
@require_admin
def admin_device_save():
    d      = request.form
    db     = get_db()
    dev_id = d.get("id", type=int)
    if dev_id:
        db.execute(
            "UPDATE known_devices SET display_name=?, mac=?, person=?, notes=?, protected=? WHERE id=?",
            (d["display_name"], d["mac"].lower(), d.get("person"), d.get("notes"),
             1 if d.get("protected") == "1" else 0, dev_id),
        )
    else:
        db.execute(
            "INSERT OR IGNORE INTO known_devices (display_name, mac, person, notes, protected) VALUES (?,?,?,?,?)",
            (d["display_name"], d["mac"].lower(), d.get("person"), d.get("notes"),
             1 if d.get("protected") == "1" else 0),
        )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/device/<int:dev_id>/delete", methods=["POST"])
@require_admin
def admin_device_delete(dev_id: int):
    get_db().execute("DELETE FROM known_devices WHERE id=?", (dev_id,))
    get_db().commit()
    return jsonify({"ok": True})


@app.route("/admin/device/<int:dev_id>/block", methods=["POST"])
@require_admin
def admin_device_block(dev_id: int):
    row = get_db().execute("SELECT mac FROM known_devices WHERE id=?", (dev_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    ok = unifi.block_device(row["mac"])
    return jsonify({"ok": ok})


@app.route("/admin/device/<int:dev_id>/unblock", methods=["POST"])
@require_admin
def admin_device_unblock(dev_id: int):
    row = get_db().execute("SELECT mac FROM known_devices WHERE id=?", (dev_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    ok = unifi.unblock_device(row["mac"])
    return jsonify({"ok": ok})


@app.route("/admin/device/<int:dev_id>/kick", methods=["POST"])
@require_admin
def admin_device_kick(dev_id: int):
    row = get_db().execute("SELECT mac FROM known_devices WHERE id=?", (dev_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    ok = unifi.kick_device(row["mac"])
    return jsonify({"ok": ok})


@app.route("/admin/devices/protect_bulk", methods=["POST"])
@require_admin
def admin_devices_protect_bulk():
    devices = json.loads(request.form.get("devices", "[]"))
    db = get_db()
    for dev in devices:
        mac     = dev.get("mac", "").lower()
        name    = dev.get("name") or mac
        protect = 1 if dev.get("protect") else 0
        if not mac:
            continue
        existing = db.execute("SELECT id FROM known_devices WHERE mac=?", (mac,)).fetchone()
        if existing:
            db.execute("UPDATE known_devices SET protected=?, display_name=? WHERE mac=?",
                       (protect, name, mac))
        elif protect:
            db.execute(
                "INSERT INTO known_devices (display_name, mac, protected) VALUES (?,?,1)",
                (name, mac),
            )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/mac/<mac>/block", methods=["POST"])
@require_admin
def admin_mac_block(mac: str):
    ok = unifi.block_device(mac)
    return jsonify({"ok": ok})


@app.route("/admin/mac/<mac>/unblock", methods=["POST"])
@require_admin
def admin_mac_unblock(mac: str):
    ok = unifi.unblock_device(mac)
    return jsonify({"ok": ok})


@app.route("/network/wifi_credentials/<ssid>", methods=["POST"])
@require_admin
def wifi_credentials(ssid: str):
    creds = unifi.get_wifi_credentials(ssid)
    if not creds:
        return jsonify({"error": "Network not found"}), 404
    # Generate QR code as base64 data URL (server-side — no CDN dependency)
    try:
        import qrcode, io, base64
        qr_string = f"WIFI:T:{creds['security']};S:{creds['ssid']};P:{creds['password']};;"
        img = qrcode.make(qr_string)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        creds["qr_data_url"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.warning("QR code generation failed: %s", e)
        creds["qr_data_url"] = None
    return jsonify(creds)


@app.route("/admin/wlan/<ssid>/toggle", methods=["POST"])
@require_admin
def admin_wlan_toggle(ssid: str):
    enabled = request.form.get("enabled", "true").lower() == "true"
    ok = unifi.set_wlan_enabled(ssid, enabled)
    return jsonify({"ok": ok})


@app.route("/admin/broadcast", methods=["POST"])
@require_admin
def admin_broadcast():
    d       = request.form
    message = d.get("message", "").strip()
    title   = d.get("title", "📢 Family Notice").strip()
    if not message:
        return jsonify({"error": "No message"}), 400
    db       = get_db()
    channels = [
        dict(r)["ntfy_channel"]
        for r in db.execute("SELECT ntfy_channel FROM person_prefs WHERE ntfy_channel IS NOT NULL AND ntfy_channel != ''").fetchall()
    ]
    count = ntfy.send_broadcast(channels, message, title=title)
    return jsonify({"ok": True, "sent": count})


@app.route("/admin/refresh_calendar", methods=["POST"])
@require_admin
def admin_refresh_calendar():
    events = calendar_sync.fetch_events(get_db())
    return jsonify({"ok": True, "count": len(events)})




# ── API: calendar data (for work PC push client) ──────────────────────────────

@app.route("/api/work_meetings")
def api_work_meetings():
    meetings = calendar_sync.get_work_meetings()
    for m in meetings:
        m["_status"] = calendar_sync.meeting_status(m)
    return jsonify(meetings)


# ── Medicine reminder background thread ──────────────────────────────────────

def _send_medicine_reminders_now():
    conn = _get_db_for_thread()
    try:
        now   = datetime.now()
        today = now.date().isoformat()
        # Window: reminders due within the last 60 seconds
        window_start = now - timedelta(seconds=60)

        meds = conn.execute(
            "SELECT m.*, pp.ntfy_channel, pp.notif_method "
            "FROM medicines m "
            "JOIN person_prefs pp ON pp.person = m.person "
            "WHERE m.active = 1"
        ).fetchall()

        for med in meds:
            med = dict(med)
            doses_per_day = int(med.get("doses_per_day") or 1)
            # Build list of dose times
            raw = med.get("dose_times")
            if raw:
                try:
                    dose_times = json.loads(raw)
                except Exception:
                    dose_times = []
            elif med.get("scheduled_time"):
                dose_times = [med["scheduled_time"]]
            else:
                continue

            for slot_i, t in enumerate(dose_times[:doses_per_day], start=1):
                if not t:
                    continue
                try:
                    h, m_val = map(int, t.split(":"))
                    sched = now.replace(hour=h, minute=m_val, second=0, microsecond=0)
                except ValueError:
                    continue

                if not (window_start <= sched <= now):
                    continue

                # Already taken?
                if conn.execute(
                    "SELECT id FROM medicine_doses "
                    "WHERE medicine_id=? AND dose_date=? AND dose_number=?",
                    (med["id"], today, slot_i)
                ).fetchone():
                    continue

                person        = med["person"]
                notif_method  = med.get("notif_method") or "ntfy"
                med_name      = med["name"]
                url           = f"{config.APP_BASE_URL}/medicines?person={person}"

                if notif_method == "push":
                    push_notif.send_push_to_person(conn, person, "💊 Medicine Reminder",
                                                   f"Time to take {med_name}", url)
                else:
                    ch = med.get("ntfy_channel")
                    if ch:
                        ntfy.send_medicine_reminder(ch, person, med_name)
    except Exception:
        log.exception("Medicine reminder job failed")
    finally:
        conn.close()


def _medicine_reminder_loop():
    _time.sleep(10)  # brief startup delay
    while True:
        try:
            _send_medicine_reminders_now()
        except Exception:
            log.exception("Medicine reminder loop error")
        _time.sleep(60)


def start_medicine_reminders():
    t = threading.Thread(target=_medicine_reminder_loop, daemon=True, name="med-reminders")
    t.start()


# ── Smart Home ────────────────────────────────────────────────────────────────

@app.route("/smarthome")
def smarthome_view():
    if current_person() not in config.ADMINS:
        return redirect(url_for("settings_view"))
    db    = get_db()
    prefs = get_prefs(db, current_person())
    rooms = [dict(r) for r in db.execute(
        "SELECT * FROM smart_rooms ORDER BY grid_row, grid_col, sort_order"
    ).fetchall()]
    devices = [dict(r) for r in db.execute(
        "SELECT * FROM smart_devices"
    ).fetchall()]
    # Attach devices to rooms
    dev_by_room: dict[int, list] = {}
    for d in devices:
        dev_by_room.setdefault(d["room_id"], []).append(d)
    for r in rooms:
        r["devices"] = dev_by_room.get(r["id"], [])
    def _setting_float(key, default):
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        try:    return float(row["value"]) if row else default
        except: return default

    def _setting_int(key, default):
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        try:    return int(row["value"]) if row else default
        except: return default

    def _img_v(filename):
        p = Path(app.static_folder) / filename
        try:    return int(p.stat().st_mtime)
        except: return 0

    return render_template(
        "smarthome.html",
        person=current_person(),
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=True,
        rooms=rooms,
        tapo_configured=bool(config.TAPO_EMAIL),
        hive_configured=bool(config.HIVE_EMAIL),
        zone_temp_min=_setting_float("zone_temp_min", 17.5),
        zone_temp_max=_setting_float("zone_temp_max", 19.0),
        grid_cols_ground=_setting_int("sh_grid_ground_cols", 10),
        grid_rows_ground=_setting_int("sh_grid_ground_rows", 15),
        grid_cols_first =_setting_int("sh_grid_first_cols",  10),
        grid_rows_first =_setting_int("sh_grid_first_rows",  15),
        fp_v_ground=_img_v("images/floorplan-ground.png"),
        fp_v_first=_img_v("images/floorplan-first.png"),
        fp_v_ground_zones=_img_v("images/floorplan-ground-zones.png"),
        fp_v_first_zones=_img_v("images/floorplan-first-zones.png"),
    )


@app.route("/smarthome/status")
def smarthome_status():
    """Live poll — returns room states with Tapo + Hive data merged."""
    if current_person() not in config.ADMINS:
        return jsonify({"error": "Admin only"}), 403

    # Serve cached data if still fresh (30-second TTL)
    _cached = _pcache_get("smarthome_status", 30)
    if _cached is not None:
        return jsonify(_cached)

    db = get_db()

    # Load room → device assignments
    rooms = [dict(r) for r in db.execute(
        "SELECT * FROM smart_rooms ORDER BY grid_row, grid_col, sort_order"
    ).fetchall()]
    assignments = [dict(r) for r in db.execute(
        "SELECT * FROM smart_devices"
    ).fetchall()]

    # Fetch live data
    tapo_devices = {d["deviceId"]: d for d in tapo.get_all_device_states()} \
        if config.TAPO_EMAIL else {}
    hive_zones   = {z["id"]: z for z in hive.get_climate_data()} \
        if config.HIVE_EMAIL else {}

    # HA entity states (batch fetch if HA is configured)
    _ha_entity_ids = [
        a["ha_entity_id"] for a in assignments
        if a.get("ha_entity_id")
    ]
    ha_states = ha_module.get_all_entity_states(_ha_entity_ids) \
        if ha_module.is_configured() and _ha_entity_ids else {}

    # Temperature trend from logger — last 2 readings per zone.
    # Avoids ROW_NUMBER() window function for maximum SQLite compatibility.
    _TLOGDB = Path(__file__).parent / "data" / "temperature_log.db"
    trend_map: dict[str, str | None] = {}
    if _TLOGDB.exists():
        try:
            _tc = sqlite3.connect(_TLOGDB)
            _rows = _tc.execute(
                "SELECT name, temperature FROM temperature_log "
                "WHERE source='hive' AND recorded_at >= datetime('now','-2 hours') "
                "ORDER BY name, recorded_at DESC"
            ).fetchall()
            _tc.close()
            _by_name: dict[str, list] = {}
            for _r in _rows:
                lst = _by_name.setdefault(_r[0], [])
                if len(lst) < 2:
                    lst.append(_r[1])
            for _name, _temps in _by_name.items():
                if len(_temps) >= 2 and _temps[0] is not None and _temps[1] is not None:
                    _diff = _temps[0] - _temps[1]   # latest minus previous
                    trend_map[_name] = "up" if _diff > 0.05 else "down" if _diff < -0.05 else "flat"
        except Exception as exc:
            log.warning("smarthome_status trend query failed: %s", exc)

    result = []
    for room in rooms:
        room_id  = room["id"]
        room_devs = [a for a in assignments if a["room_id"] == room_id]
        tapo_rows = []
        hive_row  = None
        any_on    = False

        for d in room_devs:
            if d["provider"] == "tapo":
                ha_eid = d.get("ha_entity_id")
                if ha_eid and ha_eid in ha_states:
                    # HA state takes priority — more reliable than Tapo cloud
                    on     = ha_states[ha_eid]
                    online = True
                else:
                    live   = tapo_devices.get(d["device_id"], {})
                    on     = live.get("on")
                    online = live.get("online", False)
                if on:
                    any_on = True
                tapo_rows.append({
                    "id":           d["id"],
                    "name":         d["name"],
                    "device_id":    d["device_id"],
                    "ha_entity_id": ha_eid,
                    "on":           on,
                    "online":       online,
                })
            elif d["provider"] == "hive":
                z = hive_zones.get(d["device_id"])
                if z:
                    hive_row = {**z, "trend": trend_map.get(d["name"])}

        result.append({
            "id":           room_id,
            "name":         room["name"],
            "icon":         room["icon"],
            "floor":        room.get("floor", "ground"),
            "grid_col":     room["grid_col"],
            "grid_row":     room["grid_row"],
            "grid_col_span": room["grid_col_span"],
            "grid_row_span": room["grid_row_span"],
            "zone_color":   room.get("zone_color"),
            "any_on":       any_on,
            "tapo":         tapo_rows,
            "hive":         hive_row,
        })

    wx           = weather.get_weather()
    outdoor_temp = wx.get("current", {}).get("temp")
    _out = {"rooms": result, "outdoor_temp": outdoor_temp}
    _pcache_set("smarthome_status", _out)
    return jsonify(_out)


@app.route("/smarthome/device/<int:device_db_id>/toggle", methods=["POST"])
@require_admin
def smarthome_toggle(device_db_id: int):
    db  = get_db()
    row = db.execute("SELECT * FROM smart_devices WHERE id=?", (device_db_id,)).fetchone()
    if not row or row["provider"] != "tapo":
        return jsonify({"error": "Device not found"}), 404

    # JS sends desired state in body: {on: true/false}
    # Fall back to flipping the cached state (or default to ON if unknown)
    body = request.get_json(silent=True) or {}
    desired_on = body.get("on")
    if desired_on is None:
        cached_dev = next(
            (d for d in tapo.get_all_device_states() if d["deviceId"] == row["device_id"]),
            {},
        )
        current = cached_dev.get("on")
        desired_on = (not current) if current is not None else True

    dev = next(
        (d for d in tapo.list_cloud_devices() if d["deviceId"] == row["device_id"]),
        None,
    )
    if not dev:
        return jsonify({"error": "Device not found in Tapo cloud"}), 404

    # Prefer HA over Tapo cloud if this device has an entity ID configured
    ha_eid = row["ha_entity_id"] if "ha_entity_id" in row.keys() else None
    if ha_eid and ha_module.is_configured():
        ok, err = ha_module.set_entity_state(ha_eid, bool(desired_on))
    else:
        ok, err = tapo.set_device_state(dev, bool(desired_on))
    if ok:
        _pcache_bust("smarthome_status")
    return jsonify({"ok": ok, "now_on": bool(desired_on), **({"error": err} if err else {})})


@app.route("/smarthome/timeline")
def smarthome_timeline():
    """Return historical temperature readings grouped into 15-min frames."""
    if current_person() not in config.ADMINS:
        return jsonify({"error": "Admin only"}), 403

    date_str = request.args.get("date", date.today().isoformat())
    try:
        date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    _TLOGDB = Path(__file__).parent / "data" / "temperature_log.db"
    if not _TLOGDB.exists():
        return jsonify({"frames": []})

    try:
        with sqlite3.connect(str(_TLOGDB)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT recorded_at, source, name, temperature, is_heating "
                "FROM temperature_log WHERE recorded_at LIKE ? ORDER BY recorded_at",
                (date_str + "%",),
            ).fetchall()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # Group by minute-level key — all readings in one 15-min cron run are
    # written within a few seconds of each other, so they share the same minute.
    by_minute: dict = {}
    for row in rows:
        key = row["recorded_at"][:16]   # "YYYY-MM-DDTHH:MM"
        if key not in by_minute:
            by_minute[key] = {"t": row["recorded_at"], "zones": {}}
        by_minute[key]["zones"][row["name"]] = {
            "temp":    row["temperature"],
            "heating": bool(row["is_heating"]),
            "source":  row["source"],
        }

    return jsonify({"frames": [v for _, v in sorted(by_minute.items())]})


# ── Smart home admin ──────────────────────────────────────────────────────────

@app.route("/admin/smarthome/rooms", methods=["POST"])
@require_admin
def admin_smarthome_save_room():
    db = get_db()
    d  = request.json or {}
    room_id = d.get("id")
    fields  = (
        d.get("name", "Room"),
        d.get("icon", "🏠"),
        d.get("floor", "ground"),
        int(d.get("sort_order", 0)),
        int(d.get("grid_col", 0)),
        int(d.get("grid_row", 0)),
        int(d.get("grid_col_span", 1)),
        int(d.get("grid_row_span", 1)),
        d.get("zone_color") or None,
    )
    if room_id:
        db.execute(
            "UPDATE smart_rooms SET name=?,icon=?,floor=?,sort_order=?,grid_col=?,"
            "grid_row=?,grid_col_span=?,grid_row_span=?,zone_color=? WHERE id=?",
            fields + (room_id,),
        )
    else:
        db.execute(
            "INSERT INTO smart_rooms (name,icon,floor,sort_order,grid_col,grid_row,"
            "grid_col_span,grid_row_span,zone_color) VALUES (?,?,?,?,?,?,?,?,?)",
            fields,
        )
        room_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    return jsonify({"ok": True, "id": room_id})


@app.route("/admin/smarthome/rooms/<int:room_id>/delete", methods=["POST"])
@require_admin
def admin_smarthome_delete_room(room_id: int):
    db = get_db()
    db.execute("DELETE FROM smart_devices WHERE room_id=?", (room_id,))
    db.execute("DELETE FROM smart_rooms WHERE id=?", (room_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/smarthome/discover", methods=["POST"])
@require_admin
def admin_smarthome_discover():
    """Return discovered Tapo + Hive devices plus full UniFi client list."""

    # ── UniFi lookup (MAC → IP / SSID) ───────────────────────────────────────
    def _norm_mac(m: str) -> str:
        """Normalise any MAC format to lower-case colon-separated."""
        m = m.lower().replace("-", ":").replace(".", ":").replace(" ", "")
        if len(m) == 12:  # no separators e.g. aabbccddeeff
            m = ":".join(m[i:i+2] for i in range(0, 12, 2))
        return m

    unifi_clients = unifi.list_connected_clients()
    unifi_by_mac  = {_norm_mac(c["mac"]): c for c in unifi_clients}

    # ── Tapo cloud devices ────────────────────────────────────────────────────
    tapo_devs = []
    if config.TAPO_EMAIL:
        for d in tapo.list_cloud_devices():
            raw_mac = d.get("deviceMac", "")
            mac     = _norm_mac(raw_mac) if raw_mac else ""
            client  = unifi_by_mac.get(mac, {})
            tapo_devs.append({
                "provider":    "tapo",
                "device_id":   d.get("deviceId", ""),
                "name":        d.get("alias", d.get("deviceName", "Device")),
                "device_type": d.get("deviceModel", ""),
                "mac":         mac,
                "ip":          client.get("ip", ""),
                "essid":       client.get("essid", ""),
                "online":      d.get("status") == 1,
            })

    # ── Hive heating zones ────────────────────────────────────────────────────
    hive_devs = []
    if config.HIVE_EMAIL:
        for z in hive.get_climate_data():
            hive_devs.append({
                "provider":    "hive",
                "device_id":   z["id"],
                "name":        z["name"],
                "device_type": z["type"],
                "online":      z.get("online", True),
            })

    # ── All UniFi network clients (for IoT identification) ───────────────────
    network_devs = []
    for c in unifi_clients:
        network_devs.append({
            "mac":      c["mac"],
            "ip":       c.get("ip", ""),
            "hostname": c.get("hostname") or c["mac"],
            "essid":    c.get("essid", ""),
            "ap":       c.get("ap_name", ""),
            "is_wired": c.get("is_wired", False),
        })
    # Sort wired last, then by SSID then hostname
    network_devs.sort(key=lambda x: (x["is_wired"], x["essid"], x["hostname"]))

    return jsonify({"tapo": tapo_devs, "hive": hive_devs, "network": network_devs})


@app.route("/admin/smarthome/assign", methods=["POST"])
@require_admin
def admin_smarthome_assign():
    """Assign (or unassign) a discovered device to a room."""
    db      = get_db()
    d       = request.json or {}
    provider    = d.get("provider")
    device_id   = d.get("device_id")
    name        = d.get("name", "Device")
    device_type = d.get("device_type", "")
    room_id     = d.get("room_id")       # None = unassign
    ha_entity_id = d.get("ha_entity_id") or None

    existing = db.execute(
        "SELECT id FROM smart_devices WHERE provider=? AND device_id=?",
        (provider, device_id),
    ).fetchone()

    if room_id is None:
        if existing:
            db.execute("DELETE FROM smart_devices WHERE id=?", (existing["id"],))
    elif existing:
        db.execute(
            "UPDATE smart_devices SET name=?,device_type=?,room_id=?,ha_entity_id=? WHERE id=?",
            (name, device_type, room_id, ha_entity_id, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO smart_devices (provider,device_id,name,device_type,room_id,ha_entity_id)"
            " VALUES (?,?,?,?,?,?)",
            (provider, device_id, name, device_type, room_id, ha_entity_id),
        )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/smarthome/settings", methods=["POST"])
@require_admin
def admin_smarthome_settings():
    """Save global smart home settings (zone temp range, grid sizes, etc.)."""
    db = get_db()
    d  = request.json or {}
    float_keys = ("zone_temp_min", "zone_temp_max")
    int_keys   = ("sh_grid_ground_cols", "sh_grid_ground_rows",
                  "sh_grid_first_cols",  "sh_grid_first_rows")
    for key in float_keys:
        val = d.get(key)
        if val is not None:
            try:
                db.execute("INSERT OR REPLACE INTO app_settings (key,value) VALUES (?,?)",
                           (key, str(float(val))))
            except (TypeError, ValueError):
                pass
    for key in int_keys:
        val = d.get(key)
        if val is not None:
            try:
                db.execute("INSERT OR REPLACE INTO app_settings (key,value) VALUES (?,?)",
                           (key, str(int(val))))
            except (TypeError, ValueError):
                pass
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/smarthome/rooms/<int:room_id>/position", methods=["POST"])
@require_admin
def admin_smarthome_room_position(room_id: int):
    """Lightweight position-only update — used by drag-and-drop editor."""
    db = get_db()
    d  = request.json or {}
    db.execute(
        "UPDATE smart_rooms SET grid_col=?,grid_row=?,grid_col_span=?,grid_row_span=? WHERE id=?",
        (int(d.get("grid_col", 0)), int(d.get("grid_row", 0)),
         int(d.get("grid_col_span", 1)), int(d.get("grid_row_span", 1)),
         room_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/smarthome/seed", methods=["POST"])
@require_admin
def admin_smarthome_seed():
    """Pre-populate rooms from the house floor plan. Fails if rooms already exist."""
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM smart_rooms").fetchone()[0]
    if count:
        return jsonify({"ok": False, "error": f"{count} rooms already exist — delete them first"})

    seed = [
        # Ground floor (from shared floor plan)
        ("Kitchen",      "🍳", "ground", 0, 0, 1, 1),
        ("Dining Room",  "🍽️", "ground", 1, 0, 3, 1),
        ("WC",           "🚽", "ground", 0, 1, 1, 1),
        ("Lounge",       "🛋️", "ground", 1, 1, 3, 1),
        # First floor (from shared floor plan)
        ("Bedroom 4",    "🛏️", "first",  0, 0, 1, 1),
        ("Bathroom",     "🚿", "first",  1, 0, 1, 1),
        ("Bedroom 2",    "🛏️", "first",  2, 0, 2, 1),
        ("Bedroom 3",    "🛏️", "first",  0, 1, 2, 1),
        ("Bedroom 1",    "🛏️", "first",  2, 1, 2, 1),
    ]
    for name, icon, floor, col, row, col_span, row_span in seed:
        db.execute(
            "INSERT INTO smart_rooms (name,icon,floor,sort_order,grid_col,grid_row,"
            "grid_col_span,grid_row_span) VALUES (?,?,?,0,?,?,?,?)",
            (name, icon, floor, col, row, col_span, row_span),
        )
    db.commit()
    return jsonify({"ok": True, "seeded": len(seed)})


# ── Energy / Temperature history ─────────────────────────────────────────────

@app.route("/energy")
def energy_view():
    if current_person() not in config.ADMINS:
        return redirect(url_for("settings_view"))
    db    = get_db()
    prefs = get_prefs(db, current_person())
    return render_template(
        "energy.html",
        person=current_person(),
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=True,
    )


@app.route("/energy/data")
def energy_data():
    if current_person() not in config.ADMINS:
        return jsonify({"error": "forbidden"}), 403

    # Serve cached data if fresh (5-minute TTL — data logger runs every 15 min)
    _cached = _pcache_get("energy_data", 300)
    if _cached is not None:
        return jsonify(_cached)

    TEMP_DB   = Path(__file__).parent / "data" / "temperature_log.db"
    ENERGY_DB = Path(__file__).parent / "data" / "energy.db"

    # ── Build shared 15-min UTC timeline ────────────────────────────────────
    # Start from the earliest temperature reading so solar doesn't show
    # a blank period before the logger existed.  Cap at 48 h ago.
    _now  = datetime.utcnow().replace(second=0, microsecond=0)
    _now  = _now - timedelta(minutes=_now.minute % 15)
    _start = _now - timedelta(hours=48)   # default
    if TEMP_DB.exists():
        _tc = sqlite3.connect(TEMP_DB)
        _tr = _tc.execute("SELECT MIN(recorded_at) FROM temperature_log").fetchone()
        _tc.close()
        if _tr and _tr[0]:
            _ts  = _tr[0].replace("Z", "").replace(" ", "T")
            _tdt = datetime.strptime(_ts[:19], "%Y-%m-%dT%H:%M:%S")
            # Round down to 15-min boundary
            _tdt = _tdt - timedelta(minutes=_tdt.minute % 15, seconds=_tdt.second)
            if _tdt > _start:
                _start = _tdt

    timeline: list[datetime] = []
    _t = _start
    while _t <= _now:
        timeline.append(_t)
        _t += timedelta(minutes=15)

    tl_strs = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in timeline]

    def _slot(ts_str: str) -> str:
        """Round a UTC ISO timestamp string to the nearest 15-min slot key."""
        s  = ts_str.rstrip("Z").replace(" ", "T")
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return (dt - timedelta(minutes=dt.minute % 15,
                               seconds=dt.second)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = {
        "timeline":         tl_strs,
        "outdoor_current":  None,
        "solar_current_kw": None,
        "solar_today_kwh":  None,
        "solar":            [],   # aligned kw per slot (0-filled)
        "outdoor":          [],   # aligned temp per slot (None = gap)
        "rooms":            {},   # {name: {temps:[…], heating:[…]}}
    }

    # ── Our 15-min temperature logger ────────────────────────────────────────
    if TEMP_DB.exists():
        tdb = sqlite3.connect(TEMP_DB)
        tdb.row_factory = sqlite3.Row

        row = tdb.execute(
            "SELECT temperature FROM temperature_log "
            "WHERE source='outdoor' ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        if row:
            out["outdoor_current"] = row["temperature"]

        # Outdoor — bucket into 15-min slots
        outdoor_bkt: dict[str, float] = {}
        for r in tdb.execute(
            "SELECT recorded_at, temperature FROM temperature_log "
            "WHERE source='outdoor' AND recorded_at >= datetime('now','-48 hours') "
            "ORDER BY recorded_at"
        ):
            outdoor_bkt[_slot(r["recorded_at"])] = r["temperature"]

        # Hive rooms — bucket into 15-min slots
        room_bkt: dict[str, dict[str, dict]] = {}
        for r in tdb.execute(
            "SELECT recorded_at, name, temperature, is_heating FROM temperature_log "
            "WHERE source='hive' AND recorded_at >= datetime('now','-48 hours') "
            "ORDER BY name, recorded_at"
        ):
            room_bkt.setdefault(r["name"], {})[_slot(r["recorded_at"])] = {
                "temp": r["temperature"], "h": bool(r["is_heating"])
            }

        tdb.close()

        out["outdoor"] = [outdoor_bkt.get(t) for t in tl_strs]
        for name, bkt in room_bkt.items():
            pts = [bkt.get(t, {"temp": None, "h": False}) for t in tl_strs]
            out["rooms"][name] = {
                "temps":   [p["temp"] for p in pts],
                "heating": [p["h"]    for p in pts],
            }

    # ── Energy DB (solar, synced every 15 min from Pi) ────────────────────────
    if ENERGY_DB.exists():
        edb = sqlite3.connect(ENERGY_DB)
        edb.row_factory = sqlite3.Row

        # Solar — bucket 5-min readings into 15-min slots (max power per slot)
        solar_bkt: dict[str, float] = {}
        latest_kw: float | None = None
        for r in edb.execute(
            "SELECT generation_date || 'T' || start_time_UTC AS ts, "
            "       power_kw, total_yield_kwh "
            "FROM   int_solar_today "
            "WHERE  generation_date >= date('now','-2 day') "
            "ORDER  BY generation_date, start_time_UTC"
        ):
            s = _slot(r["ts"])
            solar_bkt[s] = max(solar_bkt.get(s, 0.0), r["power_kw"] or 0.0)
            latest_kw = r["power_kw"]

        # 0-fill entire timeline (overnight = 0, not a gap)
        out["solar"] = [solar_bkt.get(t, 0.0) for t in tl_strs]

        if latest_kw is not None:
            out["solar_current_kw"] = latest_kw

        row = edb.execute(
            "SELECT ROUND(MAX(total_yield_kwh) - MIN(total_yield_kwh), 2) AS kwh "
            "FROM   int_solar_today WHERE generation_date = date('now')"
        ).fetchone()
        if row and row["kwh"] is not None:
            out["solar_today_kwh"] = row["kwh"]

        edb.close()

    # ── Floor mapping from main app DB ──────────────────────────────────────
    # Maps Hive zone name (= smart_devices.name) → floor string
    floor_map: dict[str, str] = {}
    try:
        mdb = get_db()
        for _r in mdb.execute(
            "SELECT sd.name AS hive_name, LOWER(COALESCE(sr.floor,'')) AS floor "
            "FROM smart_devices sd "
            "JOIN smart_rooms sr ON sd.room_id = sr.id "
            "WHERE sd.provider = 'hive'"
        ).fetchall():
            floor_map[_r["hive_name"]] = _r["floor"]
    except Exception:
        pass

    ground_rooms: dict = {}
    first_rooms:  dict = {}
    other_rooms:  dict = {}
    for _name, _data in out["rooms"].items():
        _fl = floor_map.get(_name, "")
        if _fl in ("ground", "ground floor", "gf"):
            ground_rooms[_name] = _data
        elif _fl in ("first", "first floor", "1st", "1st floor", "ff"):
            first_rooms[_name] = _data
        else:
            other_rooms[_name] = _data

    out["ground_rooms"] = ground_rooms
    out["first_rooms"]  = first_rooms
    out["other_rooms"]  = other_rooms

    # ── Day / night stats (chart window) ────────────────────────────────────
    # Day  = 06:00–21:00 UTC  (~07:00–22:00 BST)
    # Night= 21:00–06:00 UTC
    _day_pts:   dict[str, list] = {n: [] for n in out["rooms"]}
    _night_pts: dict[str, list] = {n: [] for n in out["rooms"]}

    for _i, _ts in enumerate(tl_strs):
        _hr = int(_ts[11:13])
        for _name, _data in out["rooms"].items():
            _temp = _data["temps"][_i]
            if _temp is None:
                continue
            (_day_pts[_name] if 6 <= _hr < 21 else _night_pts[_name]).append((_temp, _ts))

    def _stat_block(pairs: list) -> dict:
        if not pairs:
            return {"min": None, "min_t": None, "max": None, "max_t": None, "avg": None}
        lo = min(pairs, key=lambda x: x[0])
        hi = max(pairs, key=lambda x: x[0])
        return {
            "min": round(lo[0], 1), "min_t": lo[1],
            "max": round(hi[0], 1), "max_t": hi[1],
            "avg": round(sum(p[0] for p in pairs) / len(pairs), 1),
        }

    out["room_stats"] = {
        _name: {
            "day":         _stat_block(_day_pts[_name]),
            "night":       _stat_block(_night_pts[_name]),
            "current_temp": next((t for t in reversed(out["rooms"][_name]["temps"]) if t is not None), None),
        }
        for _name in out["rooms"]
    }

    # ── Trend: direction between the last two non-null readings ─────────────
    # 0.05 °C threshold — small enough to catch typical Hive sensor resolution
    for _name, _data in out["rooms"].items():
        _recent = [t for t in _data["temps"] if t is not None]
        if len(_recent) >= 2:
            _diff = _recent[-1] - _recent[-2]
            _trend = "up" if _diff > 0.05 else "down" if _diff < -0.05 else "flat"
        else:
            _trend = None
        out["room_stats"][_name]["trend"] = _trend

    # ── Shared y-axis range across all room datasets ─────────────────────────
    _all_temps = [t for _d in out["rooms"].values() for t in _d["temps"] if t is not None]
    if _all_temps:
        out["y_min"] = math.floor(min(_all_temps)) - 1
        out["y_max"] = math.ceil(max(_all_temps))  + 1
    else:
        out["y_min"] = 14
        out["y_max"] = 25

    # ── Temperature extremes ─────────────────────────────────────────────────
    # Current = last non-null reading per room
    _cur: dict[str, float] = {}
    for _name, _data in out["rooms"].items():
        for _t in reversed(_data["temps"]):
            if _t is not None:
                _cur[_name] = _t
                break

    # Period max/min (whole chart window)
    _period_max: dict[str, float] = {}
    _period_min: dict[str, float] = {}
    for _name, _data in out["rooms"].items():
        _ts = [t for t in _data["temps"] if t is not None]
        if _ts:
            _period_max[_name] = max(_ts)
            _period_min[_name] = min(_ts)

    if _cur:
        _hot_now  = max(_cur, key=_cur.get)
        _cold_now = min(_cur, key=_cur.get)
        _hot_day  = max(_period_max, key=_period_max.get) if _period_max else _hot_now
        _cold_day = min(_period_min, key=_period_min.get) if _period_min else _cold_now
        out["extremes"] = {
            "hot_now":  {"room": _hot_now,  "temp": round(_cur[_hot_now],  1)},
            "cold_now": {"room": _cold_now, "temp": round(_cur[_cold_now], 1)},
            "diff_now": round(_cur[_hot_now] - _cur[_cold_now], 1),
            "hot_day":  {"room": _hot_day,  "temp": round(_period_max[_hot_day],  1)},
            "cold_day": {"room": _cold_day, "temp": round(_period_min[_cold_day], 1)},
            "diff_day": round(_period_max[_hot_day] - _period_min[_cold_day], 1),
        }
    else:
        out["extremes"] = None

    _pcache_set("energy_data", out)
    return jsonify(out)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    with app.app_context():
        tasks.ensure_chores_scheduled(get_db())
    calendar_sync.start_background_sync(_get_db_for_thread)
    start_medicine_reminders()
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
