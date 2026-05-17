"""Collis Family Planner — main Flask application."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask, g, jsonify, redirect, render_template, request,
    session, url_for,
)

import config
from modules import (
    alexa, calendar_sync, meals, medicines, ntfy, school_terms, tasks, unifi, weather,
)

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    if "db" not in g:
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


def _get_db_for_thread() -> sqlite3.Connection:
    """Open a plain connection for background threads (no Flask context)."""
    db_path = Path(config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS person_prefs (
                person          TEXT PRIMARY KEY,
                completed_style TEXT NOT NULL DEFAULT 'fade',
                ntfy_channel    TEXT,
                widget_order    TEXT,
                theme           TEXT DEFAULT 'default'
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS calendar_events (
                id           TEXT PRIMARY KEY,
                title        TEXT,
                start_dt     TEXT,
                end_dt       TEXT,
                colour       TEXT,
                all_day      INTEGER DEFAULT 0,
                attendees    TEXT,
                cached_at    TEXT,
                first_seen_at TEXT,
                cancelled    INTEGER DEFAULT 0
            );
            -- Migrate existing installs: add columns if missing
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
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                title                TEXT NOT NULL,
                assignee             TEXT NOT NULL DEFAULT 'anyone',
                due_date             TEXT,
                notes                TEXT,
                is_chore             INTEGER DEFAULT 0,
                chore_template_id    INTEGER REFERENCES chore_templates(id),
                chore_interval_days  INTEGER,
                created_at           TEXT,
                deferred_to          TEXT,
                deferred_reason      TEXT,
                completed_by         TEXT,
                completed_at         TEXT,
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
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                name                  TEXT NOT NULL,
                person                TEXT NOT NULL,
                daily_dose            REAL  DEFAULT 1,
                stock_count           REAL  DEFAULT 0,
                reorder_threshold_days INTEGER DEFAULT 14,
                last_ordered          TEXT,
                notes                 TEXT
            );

            CREATE TABLE IF NOT EXISTS medicine_doses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
                taken_by    TEXT,
                taken_at    TEXT,
                dose_date   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS known_devices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                mac         TEXT NOT NULL UNIQUE,
                person      TEXT,
                notes       TEXT
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
        """)
        db.commit()

        # Seed default prefs for each person
        for person in config.PEOPLE:
            db.execute(
                "INSERT OR IGNORE INTO person_prefs (person) VALUES (?)", (person,)
            )
        db.commit()

        # Seed default chores
        tasks.seed_default_chores(db)

        # Add theme column if missing (migration for existing DBs)
        cols = [r[1] for r in db.execute("PRAGMA table_info(person_prefs)").fetchall()]
        if "theme" not in cols:
            db.execute("ALTER TABLE person_prefs ADD COLUMN theme TEXT DEFAULT 'default'")
            db.commit()

        # Paul prefers dark mode by default
        db.execute(
            "UPDATE person_prefs SET theme='dark' WHERE person='paul' AND (theme IS NULL OR theme='default')"
        )

        # Seed env-var NTFY channels if provided
        for person, ch in [
            ("paul",  config.NTFY_CHANNEL_PAUL),
            ("katie", config.NTFY_CHANNEL_KATIE),
        ]:
            if ch:
                db.execute(
                    "UPDATE person_prefs SET ntfy_channel=? WHERE person=? AND ntfy_channel IS NULL",
                    (ch, person),
                )
        db.commit()

    log.info("Database initialised at %s", config.DB_PATH)


# ── Helpers ───────────────────────────────────────────────────────────────────

@app.before_request
def sync_person_from_args():
    """If ?person= is in the URL (e.g. NTFY deep-links), update the session."""
    p = request.args.get("person")
    if p and p in config.PEOPLE + ["family"]:
        session["person"] = p


def current_person() -> str:
    return session.get("person", "family")


def get_prefs(db, person: str) -> dict:
    row = db.execute("SELECT * FROM person_prefs WHERE person=?", (person,)).fetchone()
    return dict(row) if row else {"completed_style": "fade"}


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_person() not in config.ADMINS:
            return jsonify({"error": "Admin only"}), 403
        pin = request.headers.get("X-Admin-Pin") or request.form.get("admin_pin") or request.args.get("admin_pin")
        if pin != config.ADMIN_PIN:
            return jsonify({"error": "Invalid PIN"}), 403
        return f(*args, **kwargs)
    return decorated


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

    today_events    = calendar_sync.get_today_events(db, person)
    work_meetings   = calendar_sync.get_work_meetings() if person in ("paul", "family") else []
    leave_checklist = calendar_sync.before_you_leave(db, person)
    today_tasks     = tasks.get_tasks_for_person(db, person)
    today_meds      = medicines.get_today_doses(db, person)
    wx              = weather.get_weather()
    in_term         = school_terms.is_term_time()

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
        in_term=in_term,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        today=date.today().isoformat(),
        is_admin=person in config.ADMINS,
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
    shopping   = meals.get_shopping_list(db, week_start)

    prev_week = (date.fromisoformat(week_start) - timedelta(days=7)).isoformat()
    next_week = (date.fromisoformat(week_start) + timedelta(days=7)).isoformat()

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
        meal_types=["Breakfast", "Lunch", "Dinner"],
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
        get_db(), d["item"], d.get("quantity"), d.get("category", "Other"),
        "manual", d.get("week_start"),
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
    meals.clear_checked_items(get_db(), request.form.get("week_start"))
    return jsonify({"ok": True})


# ── Alexa Shopping List ───────────────────────────────────────────────────────

@app.route("/alexa/auth")
def alexa_auth():
    if current_person() not in config.ADMINS:
        return "Admin only", 403
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
    meds  = medicines.get_today_doses(db, person)

    return render_template(
        "medicines.html",
        person=person,
        prefs=prefs,
        meds=meds,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
    )


@app.route("/medicines/<int:med_id>/take", methods=["POST"])
def medicine_take(med_id: int):
    person = current_person()
    taken  = medicines.log_dose(get_db(), med_id, person)
    return jsonify({"ok": True, "already_taken": not taken})


@app.route("/medicines/<int:med_id>/untake", methods=["POST"])
def medicine_untake(med_id: int):
    medicines.unlog_dose(get_db(), med_id)
    return jsonify({"ok": True})


@app.route("/medicines/<int:med_id>/reordered", methods=["POST"])
def medicine_reordered(med_id: int):
    new_stock = request.form.get("new_stock", type=float)
    medicines.mark_reordered(get_db(), med_id, new_stock)
    return jsonify({"ok": True})


# ── Settings (personal) ───────────────────────────────────────────────────────

@app.route("/settings")
def settings_view():
    person = current_person()
    db     = get_db()
    prefs  = get_prefs(db, person)

    google_connected = bool(
        db.execute("SELECT value FROM app_settings WHERE key='google_token'").fetchone()
    )
    known_devs = [dict(r) for r in db.execute("SELECT * FROM known_devices ORDER BY person, display_name").fetchall()]
    wlans      = unifi.list_wlans() if person in config.ADMINS else []

    return render_template(
        "settings.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
        google_connected=google_connected,
        known_devices=known_devs,
        wlans=wlans,
        managed_wlans=config.MANAGED_WLANS,
    )


@app.route("/settings/save", methods=["POST"])
def settings_save():
    person = current_person()
    d      = request.form
    db     = get_db()
    db.execute(
        """UPDATE person_prefs
           SET completed_style=?, ntfy_channel=?, theme=?
           WHERE person=?""",
        (d.get("completed_style", "fade"), d.get("ntfy_channel", ""),
         d.get("theme", "default"), person),
    )
    db.commit()
    return redirect(url_for("settings_view"))


@app.route("/settings/ntfy_test", methods=["POST"])
def ntfy_test():
    person = current_person()
    db     = get_db()
    prefs  = get_prefs(db, person)
    ch     = prefs.get("ntfy_channel")
    if not ch:
        return jsonify({"error": "No NTFY channel set"}), 400
    ok = ntfy.send_ntfy(ch, "Test from Family Planner!", title="✅ NTFY Test",
                        click_url=f"{config.APP_BASE_URL}/dashboard?person={person}")
    return jsonify({"ok": ok})


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_view():
    person = current_person()
    if person not in config.ADMINS:
        return redirect(url_for("settings_view"))
    db = get_db()
    prefs        = get_prefs(db, person)
    chore_templates = [dict(r) for r in db.execute("SELECT * FROM chore_templates ORDER BY title").fetchall()]
    all_meds     = medicines.get_medicines(db)
    known_devs   = [dict(r) for r in db.execute("SELECT * FROM known_devices ORDER BY person, display_name").fetchall()]
    wlans        = unifi.list_wlans()
    connected    = unifi.list_connected_clients()

    google_connected = bool(
        db.execute("SELECT value FROM app_settings WHERE key='google_token'").fetchone()
    )

    return render_template(
        "admin.html",
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=True,
        chore_templates=chore_templates,
        all_meds=all_meds,
        known_devices=known_devs,
        wlans=wlans,
        managed_wlans=config.MANAGED_WLANS,
        connected_clients=connected,
        google_connected=google_connected,
        admin_pin=config.ADMIN_PIN,
    )


@app.route("/admin/chore", methods=["POST"])
@require_admin
def admin_chore_save():
    d  = request.form
    db = get_db()
    chore_id = d.get("id", type=int)
    if chore_id:
        db.execute(
            "UPDATE chore_templates SET title=?, interval_days=?, default_assignee=?, active=? WHERE id=?",
            (d["title"], int(d["interval_days"]), d["assignee"], int(d.get("active", 1)), chore_id),
        )
    else:
        db.execute(
            "INSERT INTO chore_templates (title, interval_days, default_assignee) VALUES (?,?,?)",
            (d["title"], int(d["interval_days"]), d.get("assignee", "anyone")),
        )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/chore/<int:chore_id>/delete", methods=["POST"])
@require_admin
def admin_chore_delete(chore_id: int):
    get_db().execute("DELETE FROM chore_templates WHERE id=?", (chore_id,))
    get_db().commit()
    return jsonify({"ok": True})


@app.route("/admin/medicine", methods=["POST"])
@require_admin
def admin_medicine_save():
    d      = request.form
    db     = get_db()
    med_id = d.get("id", type=int)
    if med_id:
        medicines.update_medicine(
            db, med_id,
            name=d["name"], person=d["person"],
            daily_dose=float(d.get("daily_dose", 1)),
            stock_count=float(d.get("stock_count", 0)),
            reorder_threshold_days=int(d.get("reorder_threshold_days", 14)),
            notes=d.get("notes"),
        )
    else:
        medicines.add_medicine(
            db, d["name"], d["person"],
            daily_dose=float(d.get("daily_dose", 1)),
            stock_count=float(d.get("stock_count", 0)),
            reorder_threshold_days=int(d.get("reorder_threshold_days", 14)),
            notes=d.get("notes"),
        )
    return jsonify({"ok": True})


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
            "UPDATE known_devices SET display_name=?, mac=?, person=?, notes=? WHERE id=?",
            (d["display_name"], d["mac"].lower(), d.get("person"), d.get("notes"), dev_id),
        )
    else:
        db.execute(
            "INSERT OR IGNORE INTO known_devices (display_name, mac, person, notes) VALUES (?,?,?,?)",
            (d["display_name"], d["mac"].lower(), d.get("person"), d.get("notes")),
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


@app.route("/admin/refresh_terms", methods=["POST"])
@require_admin
def admin_refresh_terms():
    data = school_terms.fetch_term_dates(force=True)
    return jsonify({"ok": True, "terms": len(data.get("terms", []))})


# ── API: calendar data (for work PC push client) ──────────────────────────────

@app.route("/api/work_meetings")
def api_work_meetings():
    meetings = calendar_sync.get_work_meetings()
    for m in meetings:
        m["_status"] = calendar_sync.meeting_status(m)
    return jsonify(meetings)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    # Schedule today's chores on startup
    with app.app_context():
        tasks.ensure_chores_scheduled(get_db())
    calendar_sync.start_background_sync(_get_db_for_thread)
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
