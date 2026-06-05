"""Meal planner and shopping list management."""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)


def get_week_start(ref: date = None) -> date:
    d = ref or date.today()
    return d - timedelta(days=d.weekday())  # Monday


def get_meal_plan(db_conn, week_start: str = None) -> dict:
    """Return meal plan for a week, keyed by date then meal_type."""
    if not week_start:
        week_start = get_week_start().isoformat()
    week_end = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()

    rows = db_conn.execute(
        "SELECT * FROM meal_plan WHERE date BETWEEN ? AND ? ORDER BY date, meal_type",
        (week_start, week_end),
    ).fetchall()

    plan = {}
    for r in rows:
        d = r["date"]
        if d not in plan:
            plan[d] = {}
        plan[d][r["meal_type"]] = dict(r)
    return plan


def set_meal(db_conn, meal_date: str, meal_type: str, recipe_name: str,
             servings: int = 4, notes: str = None):
    existing = db_conn.execute(
        "SELECT id FROM meal_plan WHERE date=? AND meal_type=?",
        (meal_date, meal_type),
    ).fetchone()
    if existing:
        db_conn.execute(
            "UPDATE meal_plan SET recipe_name=?, servings=?, notes=? WHERE id=?",
            (recipe_name, servings, notes, existing["id"]),
        )
    else:
        db_conn.execute(
            "INSERT INTO meal_plan (date, meal_type, recipe_name, servings, notes) VALUES (?,?,?,?,?)",
            (meal_date, meal_type, recipe_name, servings, notes),
        )
    db_conn.commit()


def clear_meal(db_conn, meal_date: str, meal_type: str):
    db_conn.execute(
        "DELETE FROM meal_plan WHERE date=? AND meal_type=?",
        (meal_date, meal_type),
    )
    db_conn.commit()


# ── Shopping list ─────────────────────────────────────────────────────────────

def get_shopping_list(db_conn) -> list[dict]:
    rows = db_conn.execute(
        "SELECT * FROM shopping_items ORDER BY checked, category, item",
    ).fetchall()
    return [dict(r) for r in rows]


def add_shopping_item(db_conn, item: str, quantity: str = None,
                      category: str = "Other", source: str = "manual",
                      asda_product_id: str = None, is_manual: int = 0) -> int:
    db_conn.execute(
        "INSERT INTO shopping_items (item, quantity, category, source, asda_product_id, is_manual) VALUES (?,?,?,?,?,?)",
        (item, quantity, category, source, asda_product_id, is_manual),
    )
    db_conn.commit()
    return db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def check_shopping_item(db_conn, item_id: int, checked: bool = True):
    db_conn.execute(
        "UPDATE shopping_items SET checked=? WHERE id=?",
        (1 if checked else 0, item_id),
    )
    db_conn.commit()


def delete_shopping_item(db_conn, item_id: int):
    db_conn.execute("DELETE FROM shopping_items WHERE id=?", (item_id,))
    db_conn.commit()


def clear_checked_items(db_conn):
    db_conn.execute("DELETE FROM shopping_items WHERE checked=1")
    db_conn.commit()


ASDA_CATEGORIES = [
    "Fresh Fruit & Veg",
    "Meat & Fish",
    "Dairy & Eggs",
    "Bakery",
    "Frozen",
    "Tins & Packets",
    "Condiments & Sauces",
    "Snacks",
    "Drinks",
    "Household",
    "Pet",
    "Other",
]
