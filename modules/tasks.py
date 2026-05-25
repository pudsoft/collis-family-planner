"""Task management — CRUD, recurring chores, executive-function transfer, defer."""
from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta

log = logging.getLogger(__name__)

# ── Default recurring house chores ────────────────────────────────────────────
# (title, interval_days, default_assignee)
DEFAULT_CHORES = [
    ("Hoover downstairs",        7,  "anyone"),
    ("Hoover upstairs",          7,  "anyone"),
    ("Mop kitchen floor",        7,  "anyone"),
    ("Clean bathrooms",          7,  "anyone"),
    ("Change bed sheets",        14, "anyone"),
    ("Empty all bins",           7,  "anyone"),
    ("Clean oven",               30, "anyone"),
    ("Wipe down kitchen units",  7,  "anyone"),
    ("Clean fridge",             30, "anyone"),
    ("Descale kettle",           30, "anyone"),
    ("Wash dog bed",             14, "anyone"),
    ("Clean windows (inside)",   30, "anyone"),
    ("Tidy utility room",        14, "anyone"),
    ("Sort recycling",           7,  "anyone"),
    ("Water plants",             7,  "anyone"),
]


# ── Chore scheduler ───────────────────────────────────────────────────────────

def ensure_chores_scheduled(db_conn):
    """Insert chore tasks for today if they aren't already present this period."""
    today_str = date.today().isoformat()
    chores = db_conn.execute(
        "SELECT * FROM chore_templates ORDER BY title"
    ).fetchall()

    today_weekday = date.today().weekday()  # 0=Mon … 6=Sun

    for chore in chores:
        if not chore["active"]:
            continue

        repeat_days_raw = chore["repeat_days"] if "repeat_days" in chore.keys() else None

        if repeat_days_raw:
            # ── Specific weekdays mode ────────────────────────────────────────
            try:
                repeat_days = json.loads(repeat_days_raw)
            except Exception:
                repeat_days = []
            if today_weekday not in repeat_days:
                continue  # not scheduled today
            # Don't create a second instance if one already exists for today
            exists = db_conn.execute(
                "SELECT id FROM tasks WHERE chore_template_id=? AND due_date=?",
                (chore["id"], today_str),
            ).fetchone()
            if exists:
                continue
        else:
            # ── Interval (every N days) mode ──────────────────────────────────
            interval = chore["interval_days"]
            last = db_conn.execute(
                """SELECT due_date, completed_at FROM tasks
                   WHERE chore_template_id = ? AND (completed_at IS NOT NULL OR due_date >= ?)
                   ORDER BY due_date DESC LIMIT 1""",
                (chore["id"], today_str),
            ).fetchone()

            if last:
                last_due = last["due_date"] or today_str
                next_due = (date.fromisoformat(last_due) + timedelta(days=interval)).isoformat()
                if next_due > today_str:
                    continue  # not due yet
                exists = db_conn.execute(
                    "SELECT id FROM tasks WHERE chore_template_id=? AND due_date=? AND completed_at IS NULL",
                    (chore["id"], today_str),
                ).fetchone()
                if exists:
                    continue

        _create_task(db_conn, {
            "title":               chore["title"],
            "assignee":            chore["default_assignee"],
            "due_date":            today_str,
            "is_chore":            1,
            "chore_template_id":   chore["id"],
            "chore_interval_days": chore["interval_days"],
        })

    db_conn.commit()


def seed_default_chores(db_conn):
    """Insert default chore templates if the table is empty."""
    count = db_conn.execute("SELECT COUNT(*) FROM chore_templates").fetchone()[0]
    if count > 0:
        return
    db_conn.executemany(
        "INSERT INTO chore_templates (title, interval_days, default_assignee) VALUES (?,?,?)",
        DEFAULT_CHORES,
    )
    db_conn.commit()
    log.info("Seeded %d default chore templates", len(DEFAULT_CHORES))


# ── CRUD ──────────────────────────────────────────────────────────────────────

def _create_task(db_conn, data: dict) -> int:
    now = datetime.now().isoformat()
    cur = db_conn.execute(
        """INSERT INTO tasks
           (title, assignee, due_date, is_chore, chore_template_id,
            chore_interval_days, created_at)
           VALUES (:title, :assignee, :due_date, :is_chore,
                   :chore_template_id, :chore_interval_days, :created_at)""",
        {
            "title":               data.get("title", ""),
            "assignee":            data.get("assignee", "anyone"),
            "due_date":            data.get("due_date"),
            "is_chore":            data.get("is_chore", 0),
            "chore_template_id":   data.get("chore_template_id"),
            "chore_interval_days": data.get("chore_interval_days"),
            "created_at":          now,
        },
    )
    return cur.lastrowid


def create_task(db_conn, title: str, assignee: str, due_date: str = None,
                notes: str = None) -> int:
    db_conn.execute(
        "INSERT INTO tasks (title, assignee, due_date, notes, created_at) VALUES (?,?,?,?,?)",
        (title, assignee, due_date, notes, datetime.now().isoformat()),
    )
    db_conn.commit()
    return db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_tasks_for_person(db_conn, person: str, include_done: bool = False) -> list[dict]:
    """
    Return tasks relevant to a person.
    - Tasks directly assigned to them
    - 'anyone' tasks (including those exec-function-transferred)
    - For paul/katie: also show exec_function_transfer tasks where they are the recipient
    """
    today = date.today().isoformat()
    rows = db_conn.execute(
        """SELECT * FROM tasks
           WHERE (completed_at IS NULL OR :include_done)
             AND (deferred_to IS NULL OR deferred_to <= :today)
           ORDER BY
             CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
             due_date, created_at""",
        {"include_done": 1 if include_done else 0, "today": today},
    ).fetchall()

    results = []
    for r in rows:
        t = dict(r)
        assignee   = t.get("assignee", "")
        transfer   = t.get("exec_function_transfer", "")

        if assignee == person:
            t["_display_role"] = "assigned"
            results.append(t)
        elif assignee == "anyone":
            if transfer:
                # Show to the person who received the transfer AND the original person
                t["_display_role"] = "transferred_to" if transfer == person else "transferred_away"
                results.append(t)
            else:
                t["_display_role"] = "anyone"
                results.append(t)
        elif transfer == person:
            # Someone transferred this to me
            t["_display_role"] = "transferred_to"
            results.append(t)

    return results


def complete_task(db_conn, task_id: int, person: str):
    db_conn.execute(
        "UPDATE tasks SET completed_by=?, completed_at=? WHERE id=?",
        (person, datetime.now().isoformat(), task_id),
    )
    db_conn.commit()


def uncomplete_task(db_conn, task_id: int):
    db_conn.execute(
        "UPDATE tasks SET completed_by=NULL, completed_at=NULL WHERE id=?",
        (task_id,),
    )
    db_conn.commit()


def defer_task(db_conn, task_id: int, defer_to: str, reason: str = None):
    db_conn.execute(
        "UPDATE tasks SET deferred_to=?, deferred_reason=? WHERE id=?",
        (defer_to, reason, task_id),
    )
    db_conn.commit()


def transfer_exec_function(db_conn, task_id: int, from_person: str) -> str:
    """Transfer an 'anyone' task to the other admin. Returns recipient name."""
    recipient = "paul" if from_person == "katie" else "katie"
    db_conn.execute(
        "UPDATE tasks SET exec_function_transfer=? WHERE id=?",
        (recipient, task_id),
    )
    db_conn.commit()
    return recipient


def delete_task(db_conn, task_id: int):
    db_conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    db_conn.commit()


def update_task(db_conn, task_id: int, **fields):
    allowed = {"title", "assignee", "due_date", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db_conn.execute(
        f"UPDATE tasks SET {set_clause} WHERE id=?",
        list(updates.values()) + [task_id],
    )
    db_conn.commit()
