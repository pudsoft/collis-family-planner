"""Medicine inventory, daily dose tracking, and reorder alerts."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

LATE_GRACE_MINUTES = 30

log = logging.getLogger(__name__)


def _parse_dose_times(med: dict) -> list[str | None]:
    """Return list of HH:MM strings (one per dose slot), falling back to scheduled_time."""
    raw = med.get("dose_times")
    if raw:
        try:
            times = json.loads(raw)
            if isinstance(times, list):
                return times
        except (json.JSONDecodeError, TypeError):
            pass
    # Legacy single scheduled_time
    st = med.get("scheduled_time")
    return [st] if st else [None]


def _parse_monthly_schedule(med: dict) -> dict:
    """Return schedule dict {dom, time} from dose_times JSON for monthly/3monthly meds."""
    raw = med.get("dose_times")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _is_dose_due(med: dict, check_date: date) -> bool:
    """Return True if the medicine has a dose due on check_date."""
    freq = (med.get("frequency_type") or "daily").lower()
    if freq == "daily":
        return True
    sched = _parse_monthly_schedule(med)
    if freq == "monthly":
        dom = int(sched.get("dom") or 1)
        return check_date.day == dom
    if freq == "3monthly":
        start = med.get("start_date")
        if not start:
            return False
        try:
            sd = date.fromisoformat(start)
            sched = _parse_monthly_schedule(med)
            check_dom = int(sched.get("dom") or sd.day)
            months_diff = (check_date.year - sd.year) * 12 + (check_date.month - sd.month)
            return months_diff % 3 == 0 and check_date.day == check_dom
        except ValueError:
            return False
    return True


def _next_dose_date(med: dict, from_date: date) -> date | None:
    """Return the next due date on or after from_date for monthly/3monthly meds."""
    freq = (med.get("frequency_type") or "daily").lower()
    if freq == "daily":
        return None
    if freq == "monthly":
        sched = _parse_monthly_schedule(med)
        dom = min(int(sched.get("dom") or 1), 28)
        for delta in range(0, 3):
            m = from_date.month + delta
            y = from_date.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            try:
                candidate = date(y, m, dom)
                if candidate >= from_date:
                    return candidate
            except ValueError:
                pass
        return None
    if freq == "3monthly":
        start = med.get("start_date")
        if not start:
            return None
        try:
            sd = date.fromisoformat(start)
            sched = _parse_monthly_schedule(med)
            check_dom = min(int(sched.get("dom") or sd.day), 28)
            if _is_dose_due(med, from_date):
                return from_date
            months_diff = (from_date.year - sd.year) * 12 + (from_date.month - sd.month)
            cycles = (months_diff // 3) + 1
            for c in range(cycles, cycles + 4):
                m = sd.month + c * 3
                y = sd.year + (m - 1) // 12
                m = ((m - 1) % 12) + 1
                try:
                    candidate = date(y, m, check_dom)
                    if candidate >= from_date:
                        return candidate
                except ValueError:
                    pass
        except ValueError:
            pass
    return None


def _build_dose_slots(db_conn, med: dict, dose_date: str, is_today: bool) -> list[dict]:
    freq = (med.get("frequency_type") or "daily").lower()

    if freq in ("monthly", "3monthly"):
        check_date = date.fromisoformat(dose_date)
        due        = _is_dose_due(med, check_date)
        sched      = _parse_monthly_schedule(med)
        sched_time = sched.get("time") or med.get("scheduled_time")
        if due:
            row = db_conn.execute(
                "SELECT * FROM medicine_doses WHERE medicine_id=? AND dose_date=? AND dose_number=?",
                (med["id"], dose_date, 1),
            ).fetchone()
            taken    = row is not None
            taken_at = row["taken_at"] if row else None
            is_late  = False
            if is_today and sched_time and not taken:
                try:
                    h, m = map(int, sched_time.split(":"))
                    sched_dt = datetime.combine(date.today(), time(h, m))
                    is_late  = datetime.now() > sched_dt + timedelta(minutes=LATE_GRACE_MINUTES)
                except ValueError:
                    pass
            return [{"dose_number": 1, "taken": taken, "taken_at": taken_at,
                     "scheduled_time": sched_time, "is_late": is_late,
                     "is_due": True, "next_dose_date": None}]
        else:
            next_d = _next_dose_date(med, check_date)
            return [{"dose_number": 1, "taken": False, "taken_at": None,
                     "scheduled_time": None, "is_late": False,
                     "is_due": False,
                     "next_dose_date": next_d.isoformat() if next_d else None}]

    # Daily logic
    doses_per_day = int(med.get("doses_per_day") or 1)
    times = _parse_dose_times(med)
    while len(times) < doses_per_day:
        times.append(None)
    times = times[:doses_per_day]

    slots = []
    for i, sched_time in enumerate(times, start=1):
        row = db_conn.execute(
            "SELECT * FROM medicine_doses WHERE medicine_id=? AND dose_date=? AND dose_number=?",
            (med["id"], dose_date, i),
        ).fetchone()
        taken    = row is not None
        taken_at = row["taken_at"] if row else None

        is_late = False
        if is_today and sched_time and not taken:
            try:
                h, m = map(int, sched_time.split(":"))
                sched_dt = datetime.combine(date.today(), time(h, m))
                is_late  = datetime.now() > sched_dt + timedelta(minutes=LATE_GRACE_MINUTES)
            except ValueError:
                pass

        slots.append({
            "dose_number":    i,
            "taken":          taken,
            "taken_at":       taken_at,
            "scheduled_time": sched_time,
            "is_late":        is_late,
            "is_due":         True,
            "next_dose_date": None,
        })
    return slots


def _annotate_med(db_conn, med: dict, dose_date: str, is_today: bool) -> dict:
    slots = _build_dose_slots(db_conn, med, dose_date, is_today)
    med["dose_slots"]    = slots
    due_slots            = [s for s in slots if s.get("is_due", True)]
    med["taken_today"]   = bool(due_slots) and all(s["taken"] for s in due_slots)
    med["taken_at"]      = slots[0]["taken_at"] if slots else None
    med["is_late"]       = any(s["is_late"] for s in slots)
    med["is_due_today"]  = bool(due_slots)
    med["next_dose_date"] = next((s["next_dose_date"] for s in slots if s.get("next_dose_date")), None)

    # Next future due date: for due days compute the one after; for non-due days use slot value
    freq_check = (med.get("frequency_type") or "daily").lower()
    if freq_check in ("monthly", "3monthly"):
        check_d = date.fromisoformat(dose_date)
        if med["is_due_today"]:
            nfd = _next_dose_date(med, check_d + timedelta(days=1))
            med["next_future_due"] = nfd.isoformat() if nfd else None
        else:
            med["next_future_due"] = med["next_dose_date"]
    else:
        med["next_future_due"] = None

    freq          = (med.get("frequency_type") or "daily").lower()
    doses_per_day = int(med.get("doses_per_day") or 1)

    if freq == "monthly":
        per_dose = med["daily_dose"]
        med["days_remaining"] = (
            round(med["stock_count"] / per_dose * 30, 0)
            if per_dose and med["stock_count"] else None
        )
    elif freq == "3monthly":
        per_dose = med["daily_dose"]
        med["days_remaining"] = (
            round(med["stock_count"] / per_dose * 90, 0)
            if per_dose and med["stock_count"] else None
        )
    else:
        per_dose = (med["daily_dose"] / doses_per_day) if doses_per_day else med["daily_dose"]
        med["days_remaining"] = (
            round(med["stock_count"] / med["daily_dose"], 1)
            if med["daily_dose"] and med["stock_count"] else None
        )

    med["needs_reorder"] = (
        med["days_remaining"] is not None
        and med["days_remaining"] <= med["reorder_threshold_days"]
    )
    med["per_dose_amount"] = per_dose

    # Course countdown — how many days until (or since) the end of the course
    end_date = med.get("end_date")
    if end_date:
        try:
            today = date.today()
            ed = date.fromisoformat(end_date)
            med["course_days_remaining"] = (ed - today).days  # negative = ended
        except ValueError:
            med["course_days_remaining"] = None
    else:
        med["course_days_remaining"] = None

    return med


# ── CRUD ──────────────────────────────────────────────────────────────────────

def get_medicines(db_conn, person: str = None, active_only: bool = False,
                  on_date: str = None) -> list[dict]:
    """Return medicines, optionally filtered to those active on a given ISO date."""
    where = []
    params = []
    if person and person != "family":
        where.append("person=?")
        params.append(person)
    if active_only:
        where.append("active=1")
    if on_date:
        where.append("(start_date IS NULL OR start_date <= ?)")
        params.append(on_date)
        where.append("(end_date IS NULL OR end_date >= ?)")
        params.append(on_date)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db_conn.execute(
        f"SELECT * FROM medicines {clause} ORDER BY person, name", params
    ).fetchall()
    return [dict(r) for r in rows]


def add_medicine(db_conn, name: str, person: str, daily_dose: float = 1,
                 stock_count: float = 0, reorder_threshold_days: int = 14,
                 notes: str = None, scheduled_time: str = None,
                 doses_per_day: int = 1, dose_times: str = None,
                 active: int = 1, start_date: str = None,
                 end_date: str = None, frequency_type: str = "daily") -> int:
    db_conn.execute(
        """INSERT INTO medicines
           (name, person, daily_dose, stock_count, reorder_threshold_days,
            notes, scheduled_time, doses_per_day, dose_times, active,
            start_date, end_date, frequency_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, person, daily_dose, stock_count, reorder_threshold_days,
         notes, scheduled_time, doses_per_day, dose_times, active,
         start_date, end_date, frequency_type),
    )
    db_conn.commit()
    return db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_medicine(db_conn, med_id: int, **fields):
    allowed = {"name", "person", "daily_dose", "stock_count", "reorder_threshold_days",
               "notes", "last_ordered", "scheduled_time", "doses_per_day", "dose_times",
               "active", "start_date", "end_date", "frequency_type"}
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
    db_conn.execute("DELETE FROM medicine_doses WHERE medicine_id=?", (med_id,))
    db_conn.execute("DELETE FROM medicines WHERE id=?", (med_id,))
    db_conn.commit()


# ── Daily dose tracking ───────────────────────────────────────────────────────

def get_today_doses(db_conn, person: str = None) -> list[dict]:
    today = date.today().isoformat()
    meds  = get_medicines(db_conn, person, on_date=today)
    return [_annotate_med(db_conn, m, today, True) for m in meds]


def get_doses_for_date(db_conn, dose_date: str) -> list[dict]:
    meds = get_medicines(db_conn, on_date=dose_date)
    return [_annotate_med(db_conn, m, dose_date, False) for m in meds]


def log_dose(db_conn, medicine_id: int, taken_by: str,
             dose_date: str = None, dose_number: int = 1) -> bool:
    if dose_date is None:
        dose_date = date.today().isoformat()
    existing = db_conn.execute(
        "SELECT id FROM medicine_doses WHERE medicine_id=? AND dose_date=? AND dose_number=?",
        (medicine_id, dose_date, dose_number),
    ).fetchone()
    if existing:
        return False

    med = db_conn.execute("SELECT * FROM medicines WHERE id=?", (medicine_id,)).fetchone()
    if not med:
        return False
    doses_per_day = int(med["doses_per_day"] or 1) if "doses_per_day" in med.keys() else 1
    per_dose = (med["daily_dose"] / doses_per_day) if doses_per_day else med["daily_dose"]

    db_conn.execute(
        "INSERT INTO medicine_doses (medicine_id, taken_by, taken_at, dose_date, dose_number) VALUES (?,?,?,?,?)",
        (medicine_id, taken_by, datetime.now().isoformat(), dose_date, dose_number),
    )
    db_conn.execute(
        "UPDATE medicines SET stock_count = MAX(0, stock_count - ?) WHERE id=?",
        (per_dose, medicine_id),
    )
    db_conn.commit()
    return True


def unlog_dose(db_conn, medicine_id: int,
               dose_date: str = None, dose_number: int = 1) -> bool:
    if dose_date is None:
        dose_date = date.today().isoformat()
    row = db_conn.execute(
        "SELECT id FROM medicine_doses WHERE medicine_id=? AND dose_date=? AND dose_number=?",
        (medicine_id, dose_date, dose_number),
    ).fetchone()
    if not row:
        return False

    med = db_conn.execute("SELECT * FROM medicines WHERE id=?", (medicine_id,)).fetchone()
    doses_per_day = int(med["doses_per_day"] or 1) if med and "doses_per_day" in med.keys() else 1
    per_dose = (med["daily_dose"] / doses_per_day) if (med and doses_per_day) else 1

    db_conn.execute("DELETE FROM medicine_doses WHERE id=?", (row["id"],))
    db_conn.execute(
        "UPDATE medicines SET stock_count = stock_count + ? WHERE id=?",
        (per_dose, medicine_id),
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
        db_conn.execute("UPDATE medicines SET last_ordered=? WHERE id=?", (today, medicine_id))
    db_conn.commit()


def add_stock(db_conn, medicine_id: int, quantity: float):
    db_conn.execute(
        "UPDATE medicines SET stock_count = COALESCE(stock_count, 0) + ? WHERE id=?",
        (quantity, medicine_id),
    )
    db_conn.commit()


def check_reorder_alerts(db_conn) -> list[dict]:
    return [m for m in get_today_doses(db_conn) if m.get("needs_reorder")]


# ── PRN / ad-hoc logging ──────────────────────────────────────────────────────

PRN_MIN_INTERVALS = {
    "paracetamol": 4 * 60,
    "ibuprofen":   6 * 60,
    "temperature": 0,
}

PRN_MAX_DOSES_24H = {
    "paracetamol": 4,
    "ibuprofen":   4,
}


def log_prn(db_conn, person: str, prn_type: str, value: float = None):
    db_conn.execute(
        "INSERT INTO prn_log (person, type, value, logged_at) VALUES (?,?,?,?)",
        (person, prn_type, value, datetime.now().isoformat()),
    )
    db_conn.commit()


def get_prn_status(db_conn, person: str) -> dict:
    """Return safe-to-take status for each PRN med type, accounting for both
    the minimum interval between doses and the 24-hour maximum dose count."""
    now = datetime.now()
    result = {}
    for prn_type, min_interval in PRN_MIN_INTERVALS.items():
        if prn_type == "temperature":
            continue
        max_doses = PRN_MAX_DOSES_24H.get(prn_type, 4)
        cutoff_24h = (now - timedelta(hours=24)).isoformat()

        rows = db_conn.execute(
            "SELECT logged_at FROM prn_log "
            "WHERE person=? AND type=? AND logged_at > ? ORDER BY logged_at DESC",
            (person, prn_type, cutoff_24h),
        ).fetchall()

        doses_24h = len(rows)
        last_dose = datetime.fromisoformat(rows[0]["logged_at"]) if rows else None

        # Interval rule
        interval_ok = True
        interval_next = None
        if last_dose:
            elapsed_mins = (now - last_dose).total_seconds() / 60
            if elapsed_mins < min_interval:
                interval_ok = False
                interval_next = last_dose + timedelta(minutes=min_interval)

        # 24h max rule — when the oldest qualifying dose falls outside the 24h window
        max_ok = doses_24h < max_doses
        max_next = None
        if not max_ok:
            oldest = datetime.fromisoformat(rows[-1]["logged_at"])
            max_next = oldest + timedelta(hours=24)

        safe_now = interval_ok and max_ok

        candidates = [t for t in [interval_next, max_next] if t is not None]
        next_safe_dt = max(candidates) if candidates else None

        result[prn_type] = {
            "safe_now":           safe_now,
            "doses_24h":          doses_24h,
            "max_doses":          max_doses,
            "max_reached":        not max_ok,
            "interval_ok":        interval_ok,
            "next_safe_at":       next_safe_dt.strftime("%H:%M") if next_safe_dt else None,
            "seconds_until_safe": max(0, int((next_safe_dt - now).total_seconds())) if next_safe_dt else 0,
            "last_dose_at":       last_dose.isoformat() if last_dose else None,
        }
    return result


def get_prn_log(db_conn, person: str, limit: int = 20) -> list[dict]:
    rows = db_conn.execute(
        "SELECT id, person, type, value, logged_at FROM prn_log "
        "WHERE person=? ORDER BY logged_at DESC LIMIT ?",
        (person, limit),
    ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        min_interval = PRN_MIN_INTERVALS.get(row["type"], 0)
        if min_interval:
            logged  = datetime.fromisoformat(row["logged_at"])
            elapsed = (datetime.now() - logged).total_seconds() / 60
            row["minutes_ago"]  = int(elapsed)
            row["next_safe_at"] = (logged + timedelta(minutes=min_interval)).strftime("%H:%M")
            row["safe_now"]     = elapsed >= min_interval
        else:
            row["minutes_ago"]  = None
            row["next_safe_at"] = None
            row["safe_now"]     = True
        result.append(row)
    return result
