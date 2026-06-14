"""Collis Family Planner — main Flask application."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import database

from flask import Flask, g, redirect, request, session, url_for

import config
from modules import calendar_sync, medicines, ntfy, push_notif, tasks

from routes.utils import get_db, _get_db_for_thread, current_person

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


# ── Template filters ───────────────────────────────────────────────────────────

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


# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Database init helpers ──────────────────────────────────────────────────────

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
            active                 TINYINT DEFAULT 1,
            frequency_type         VARCHAR(20) DEFAULT 'daily'
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
        """CREATE TABLE IF NOT EXISTS email_accounts (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            person        TEXT NOT NULL,
            label         VARCHAR(100) NOT NULL,
            email_address VARCHAR(255) NOT NULL,
            app_password  TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS event_tasks (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            event_id     TEXT NOT NULL,
            title        VARCHAR(500) NOT NULL,
            assignee     VARCHAR(50) DEFAULT 'anyone',
            completed    TINYINT DEFAULT 0,
            completed_at VARCHAR(50),
            completed_by VARCHAR(50),
            created_at   VARCHAR(50),
            created_by   VARCHAR(50)
        )""",
        """CREATE TABLE IF NOT EXISTS birthdays (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            name           VARCHAR(200) NOT NULL,
            date_mmdd      VARCHAR(5) NOT NULL,
            remind_days    INT DEFAULT 7,
            remind_persons TEXT,
            notes          TEXT,
            last_reminded  VARCHAR(20)
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
        ("medicines",        "start_date",     "VARCHAR(20)"),
        ("medicines",        "end_date",       "VARCHAR(20)"),
        ("medicines",        "frequency_type", "VARCHAR(20) DEFAULT 'daily'"),
        ("shopping_items",   "asda_product_id", "TEXT"),
        ("shopping_items",   "is_manual",       "INTEGER DEFAULT 0"),
        ("shopping_items",   "added_by",        "TEXT"),
        ("shopping_items",   "added_at",        "TEXT"),
        ("person_prefs",     "visible_pages",   "TEXT"),
        ("birthdays",        "last_reminded",   "VARCHAR(20)"),
        ("birthdays",        "notes",           "TEXT"),
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
            active                 INTEGER DEFAULT 1,
            frequency_type         TEXT DEFAULT 'daily'
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
        CREATE TABLE IF NOT EXISTS email_accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            person        TEXT NOT NULL,
            label         TEXT NOT NULL,
            email_address TEXT NOT NULL,
            app_password  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS event_tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT NOT NULL,
            title        TEXT NOT NULL,
            assignee     TEXT DEFAULT 'anyone',
            completed    INTEGER DEFAULT 0,
            completed_at TEXT,
            completed_by TEXT,
            created_at   TEXT,
            created_by   TEXT
        );
        CREATE TABLE IF NOT EXISTS birthdays (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            date_mmdd      TEXT NOT NULL,
            remind_days    INTEGER DEFAULT 7,
            remind_persons TEXT,
            notes          TEXT,
            last_reminded  TEXT
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
        ("medicines",        "start_date",    "TEXT"),
        ("medicines",        "end_date",      "TEXT"),
        ("medicines",        "frequency_type","TEXT DEFAULT 'daily'"),
        ("person_prefs",     "visible_pages", "TEXT"),
        ("birthdays",        "last_reminded", "TEXT"),
        ("birthdays",        "notes",         "TEXT"),
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


# ── Auth / request hooks ───────────────────────────────────────────────────────

_LOGIN_EXEMPT = {
    "auth.login", "auth.login_pin", "auth.login_google", "auth.login_google_callback",
    "auth.logout", "static", "auth.service_worker", "auth.offline",
}


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


@app.before_request
def check_auth_and_person():
    if request.endpoint in _LOGIN_EXEMPT:
        return
    if not session.get("authenticated"):
        return redirect(url_for("auth.login", next=request.url))
    p = request.args.get("person")
    if p and p in config.PEOPLE + ["family"]:
        session["person"] = p


# ── Medicine reminder background thread ───────────────────────────────────────

def _send_medicine_reminders_now():
    conn = _get_db_for_thread()
    try:
        now   = datetime.now()
        today = now.date().isoformat()
        window_start = now - timedelta(seconds=60)

        meds = conn.execute(
            "SELECT m.*, pp.ntfy_channel, pp.notif_method "
            "FROM medicines m "
            "LEFT JOIN person_prefs pp ON pp.person = m.person "
            "WHERE m.active = 1"
        ).fetchall()

        for med in meds:
            med = dict(med)
            freq = (med.get("frequency_type") or "daily").lower()
            if freq in ("monthly", "3monthly"):
                if not medicines._is_dose_due(med, now.date()):
                    continue
            doses_per_day = int(med.get("doses_per_day") or 1)
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
    _time.sleep(10)
    while True:
        try:
            _send_medicine_reminders_now()
        except Exception:
            log.exception("Medicine reminder loop error")
        _time.sleep(60)


def start_medicine_reminders():
    t = threading.Thread(target=_medicine_reminder_loop, daemon=True, name="med-reminders")
    t.start()


# ── Birthday reminder background thread ───────────────────────────────────────

_birthday_last_check: str = ""


def _send_birthday_reminders_now():
    global _birthday_last_check
    today = date.today()
    today_str = today.isoformat()
    if _birthday_last_check == today_str:
        return
    _birthday_last_check = today_str

    conn = _get_db_for_thread()
    try:
        rows = conn.execute("SELECT * FROM birthdays").fetchall()
        for row in rows:
            b = dict(row)
            try:
                mm, dd = b["date_mmdd"].split("-")
                remind_days = int(b.get("remind_days") or 7)
                remind_date = date(today.year, int(mm), int(dd)) - timedelta(days=remind_days)
                if remind_date < today:
                    remind_date = date(today.year + 1, int(mm), int(dd)) - timedelta(days=remind_days)
                if remind_date != today:
                    continue
                if b.get("last_reminded") == today_str:
                    continue
            except Exception:
                continue

            persons = []
            try:
                persons = json.loads(b["remind_persons"] or "[]")
            except Exception:
                pass
            if not persons:
                continue

            birthday_date = date(today.year, int(mm), int(dd))
            if birthday_date < today:
                birthday_date = date(today.year + 1, int(mm), int(dd))
            days_away = (birthday_date - today).days
            msg = f"🎂 {b['name']}'s birthday is in {days_away} day{'s' if days_away != 1 else ''}!"

            for person in persons:
                pp = conn.execute(
                    "SELECT notif_method, ntfy_channel FROM person_prefs WHERE person=?",
                    (person,)
                ).fetchone()
                if not pp:
                    continue
                pp = dict(pp)
                if pp.get("notif_method") == "push":
                    push_notif.send_push_to_person(
                        conn, person, "🎂 Birthday Reminder", msg,
                        f"{config.APP_BASE_URL}/calendar?person={person}"
                    )
                elif pp.get("ntfy_channel"):
                    ntfy.send_ntfy(pp["ntfy_channel"], msg, title="🎂 Birthday Reminder",
                                   click_url=f"{config.APP_BASE_URL}/calendar?person={person}")

            conn.execute(
                "UPDATE birthdays SET last_reminded=? WHERE id=?", (today_str, b["id"])
            )
            conn.commit()
    except Exception:
        log.exception("Birthday reminder job failed")
    finally:
        conn.close()


def _birthday_reminder_loop():
    _time.sleep(30)
    while True:
        try:
            _send_birthday_reminders_now()
        except Exception:
            log.exception("Birthday reminder loop error")
        _time.sleep(3600)


def start_birthday_reminders():
    t = threading.Thread(target=_birthday_reminder_loop, daemon=True, name="bday-reminders")
    t.start()


# ── Blueprint registration ─────────────────────────────────────────────────────

from routes.auth      import bp as auth_bp
from routes.dashboard import bp as dashboard_bp
from routes.calendar  import bp as calendar_bp
from routes.tasks     import bp as tasks_bp
from routes.shopping  import bp as shopping_bp
from routes.medicines import bp as medicines_bp
from routes.settings  import bp as settings_bp
from routes.network   import bp as network_bp
from routes.admin     import bp as admin_bp
from routes.smarthome     import bp as smarthome_bp
from routes.energy        import bp as energy_bp
from routes.email_manager import bp as email_manager_bp

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(calendar_bp)
app.register_blueprint(tasks_bp)
app.register_blueprint(shopping_bp)
app.register_blueprint(medicines_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(network_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(smarthome_bp)
app.register_blueprint(energy_bp)
app.register_blueprint(email_manager_bp)


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    with app.app_context():
        tasks.ensure_chores_scheduled(get_db())
    calendar_sync.start_background_sync(_get_db_for_thread)
    start_medicine_reminders()
    start_birthday_reminders()
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
