"""Medicine inventory, daily dose tracking, and reorder alerts."""

import logging
from datetime import date, datetime

log = logging.getLogger(__name__)


def get_medicines(db_conn, person: str = None) -> list[dict]:
    if person and person != "family":
        rows = db_conn.execute(
            "SELECT * FROM medicines WHERE person=? ORDER BY person, name",
            (person,),
        ).fetchall()
    else:
        rows = db_conn.execute(
            "SELECT * FROM medicines ORDER BY person, name"
        ).fetchall()
    return [dict(r) for r in rows]


def add_medicine(db_conn, name: str, person: str, daily_dose: float = 1,
                 stock_count: float = 0, reorder_threshold_days: int = 14,
                 notes: str = None) -> int:
    db_conn.execute(
        """INSERT INTO medicines
           (name, person, daily_dose, stock_count, reorder_threshold_days, notes)
           VALUES (?,?,?,?,?,?)""",
        (name, person, daily_dose, stock_count, reorder_threshold_days, notes),
    )
    db_conn.commit()
    return db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_medicine(db_conn, med_id: int, **fields):
    allowed = {"name", "person", "daily_dose", "stock_count",
               "reorder_threshold_days", "notes", "last_ordered"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db_conn.execute(
        f"UPDATE medicines SET {set_clause} WHERE id=?",
        list(updates.values()) + [med_id],
    )
    db_conn.commit()


def delete_medicine(db_conn, med_id: int):
    db_conn.execute("DELETE FROM medicines WHERE id=?", (med_id,))
    db_conn.execute("DELETE FROM medicine_doses WHERE medicine_id=?", (med_id,))
    db_conn.commit()


# ── Daily dose tracking ───────────────────────────────────────────────────────

def get_today_doses(db_conn, person: str = None) -> list[dict]:
    """Return medicines with today's taken status for the given person."""
    today = date.today().isoformat()
    meds = get_medicines(db_conn, person)
    for med in meds:
        dose = db_conn.execute(
            "SELECT * FROM medicine_doses WHERE medicine_id=? AND dose_date=?",
            (med["id"], today),
        ).fetchone()
        med["taken_today"] = dose is not None
        med["taken_at"]    = dose["taken_at"] if dose else None
        med["days_remaining"] = (
            round(med["stock_count"] / med["daily_dose"], 1)
            if med["daily_dose"] and med["stock_count"]
            else None
        )
        med["needs_reorder"] = (
            med["days_remaining"] is not None
            and med["days_remaining"] <= med["reorder_threshold_days"]
        )
    return meds


def log_dose(db_conn, medicine_id: int, taken_by: str) -> bool:
    today = date.today().isoformat()
    now   = datetime.now().isoformat()
    existing = db_conn.execute(
        "SELECT id FROM medicine_doses WHERE medicine_id=? AND dose_date=?",
        (medicine_id, today),
    ).fetchone()
    if existing:
        return False  # already logged today

    db_conn.execute(
        "INSERT INTO medicine_doses (medicine_id, taken_by, taken_at, dose_date) VALUES (?,?,?,?)",
        (medicine_id, taken_by, now, today),
    )
    # Decrement stock
    db_conn.execute(
        "UPDATE medicines SET stock_count = MAX(0, stock_count - daily_dose) WHERE id=?",
        (medicine_id,),
    )
    db_conn.commit()
    return True


def unlog_dose(db_conn, medicine_id: int) -> bool:
    today = date.today().isoformat()
    dose = db_conn.execute(
        "SELECT id FROM medicine_doses WHERE medicine_id=? AND dose_date=?",
        (medicine_id, today),
    ).fetchone()
    if not dose:
        return False
    db_conn.execute("DELETE FROM medicine_doses WHERE id=?", (dose["id"],))
    # Restore stock
    db_conn.execute(
        "UPDATE medicines SET stock_count = stock_count + daily_dose WHERE id=?",
        (medicine_id,),
    )
    db_conn.commit()
    return True


def mark_reordered(db_conn, medicine_id: int, new_stock: float = None):
    today = date.today().isoformat()
    if new_stock is not None:
        db_conn.execute(
            "UPDATE medicines SET last_ordered=?, stock_count=? WHERE id=?",
            (today, new_stock, medicine_id),
        )
    else:
        db_conn.execute(
            "UPDATE medicines SET last_ordered=? WHERE id=?",
            (today, medicine_id),
        )
    db_conn.commit()


def check_reorder_alerts(db_conn) -> list[dict]:
    """Return medicines that need reordering."""
    meds = get_today_doses(db_conn)
    return [m for m in meds if m.get("needs_reorder")]
