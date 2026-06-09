"""Shopping blueprint — /meals, /shopping, /alexa/* routes."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

import config
from modules import alexa, meals
from routes.utils import current_person, get_db, get_prefs, require_admin

log = logging.getLogger(__name__)

bp = Blueprint("shopping", __name__)


def _week_days(week_start: date = None) -> list[str]:
    start = week_start or meals.get_week_start()
    return [(start + timedelta(days=i)).isoformat() for i in range(7)]


# ── Meals ──────────────────────────────────────────────────────────────────────

@bp.route("/meals")
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


@bp.route("/meals/set", methods=["POST"])
def meal_set():
    d = request.form
    meals.set_meal(get_db(), d["date"], d["meal_type"], d["recipe_name"],
                   int(d.get("servings", 4)), d.get("notes"))
    return jsonify({"ok": True})


@bp.route("/meals/clear", methods=["POST"])
def meal_clear():
    d = request.form
    meals.clear_meal(get_db(), d["date"], d["meal_type"])
    return jsonify({"ok": True})


# ── Shopping ───────────────────────────────────────────────────────────────────

@bp.route("/shopping")
def shopping_view():
    person = current_person()
    db     = get_db()
    prefs  = get_prefs(db, person)
    regulars_path = Path(__file__).parent.parent / "data" / "asda_regulars.json"
    regulars = json.loads(regulars_path.read_text()) if regulars_path.exists() else []
    shopping = meals.get_shopping_list(db)

    # Always sync category from regulars for ASDA items; attach dept in-memory
    reg_by_pid = {r["product_id"]: r for r in regulars}
    for item in shopping:
        if item.get("asda_product_id"):
            reg = reg_by_pid.get(item["asda_product_id"])
            if reg:
                cat = reg.get("category")
                if cat and cat != item.get("category"):
                    db.execute("UPDATE shopping_items SET category=? WHERE id=?", (cat, item["id"]))
                    item["category"] = cat
                item["dept"] = reg.get("dept") or ""
        if not item.get("dept"):
            item["dept"] = ""
    db.commit()

    return render_template(
        "shopping.html",
        regulars=regulars,
        shopping=shopping,
        person=person,
        prefs=prefs,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
        is_admin=person in config.ADMINS,
    )


@bp.route("/shopping/barcode", methods=["POST"])
def shopping_barcode():
    import urllib.request, json as _json, time as _time
    ean = request.form.get("ean", "").strip()
    if not ean:
        return jsonify({"error": "No barcode provided"}), 400

    off_req = urllib.request.Request(
        f"https://world.openfoodfacts.org/api/v0/product/{ean}.json",
        headers={"User-Agent": "CollisFamilyPlanner/1.0"},
    )
    try:
        with urllib.request.urlopen(off_req, timeout=6) as resp:
            off_data = _json.loads(resp.read())
    except Exception as e:
        return jsonify({"error": f"Barcode lookup failed: {e}"}), 502

    if off_data.get("status") != 1:
        return jsonify({"error": "Product not found for this barcode"}), 404

    product = off_data.get("product", {})
    name  = (product.get("product_name_en") or product.get("product_name") or "").strip()
    brand = (product.get("brands") or "").split(",")[0].strip()
    if not name:
        return jsonify({"error": "No product name found for this barcode"}), 404

    ALGOLIA_APP = "8I6WSKCCNV"
    ALGOLIA_KEY = "03e4272048dd17f771da37b57ff8a75e"
    STORE_ID    = "4383"
    now         = int(_time.time())
    query       = f"{brand} {name}".strip() if brand else name
    alg_filter  = (
        f"(STATUS:A OR STATUS:I) AND NOT DISPLAY_ONLINE:false "
        f"AND NOT UNTRAITED_STORES:{STORE_ID} AND STOCK.{STORE_ID}>0 AND END_DATE>{now}"
    )
    alg_payload = _json.dumps({
        "query": query, "hitsPerPage": 5, "filters": alg_filter,
        "attributesToRetrieve": ["CIN", "NAME", "PRICES", "PACK_SIZE", "PRIMARY_TAXONOMY"],
    }).encode()
    alg_req = urllib.request.Request(
        f"https://{ALGOLIA_APP.lower()}-dsn.algolia.net/1/indexes/ASDA_PRODUCTS/query",
        data=alg_payload,
        headers={
            "x-algolia-application-id": ALGOLIA_APP,
            "x-algolia-api-key": ALGOLIA_KEY,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(alg_req, timeout=6) as resp:
            alg_data = _json.loads(resp.read())
    except Exception as e:
        return jsonify({"error": f"ASDA search failed: {e}"}), 502

    candidates = []
    for h in alg_data.get("hits", []):
        prices   = h.get("PRICES", {})
        price_en = prices.get("EN", {}) if isinstance(prices, dict) else {}
        price    = price_en.get("PRICE") if isinstance(price_en, dict) else None
        taxonomy = h.get("PRIMARY_TAXONOMY", {}) or {}
        candidates.append({
            "cin":      str(h.get("CIN", "")),
            "name":     h.get("NAME", ""),
            "price":    price,
            "pack_size": h.get("PACK_SIZE", ""),
            "category": taxonomy.get("CAT_NAME"),
            "dept":     taxonomy.get("DEPT_NAME"),
        })

    return jsonify({"ean": ean, "off_name": name, "off_brand": brand, "candidates": candidates})


@bp.route("/shopping/add", methods=["POST"])
def shopping_add():
    d = request.form
    try:
        item_id = meals.add_shopping_item(
            get_db(), d["item"], d.get("quantity"), d.get("category", "Other"),
            source="asda" if d.get("asda_product_id") else "manual",
            asda_product_id=d.get("asda_product_id") or None,
            is_manual=1 if not d.get("asda_product_id") else 0,
            added_by=current_person(),
            added_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
    except Exception as e:
        log.exception("shopping_add failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "id": item_id})


@bp.route("/shopping/<int:item_id>/qty", methods=["POST"])
def shopping_qty(item_id: int):
    qty = request.form.get("quantity", "1")
    get_db().execute("UPDATE shopping_items SET quantity=? WHERE id=?", (qty, item_id))
    get_db().commit()
    return jsonify({"ok": True})


@bp.route("/shopping/<int:item_id>/check", methods=["POST"])
def shopping_check(item_id: int):
    checked = request.form.get("checked", "1") == "1"
    meals.check_shopping_item(get_db(), item_id, checked)
    return jsonify({"ok": True})


@bp.route("/shopping/<int:item_id>/delete", methods=["POST"])
def shopping_delete(item_id: int):
    meals.delete_shopping_item(get_db(), item_id)
    return jsonify({"ok": True})


@bp.route("/shopping/clear_checked", methods=["POST"])
def shopping_clear_checked():
    meals.clear_checked_items(get_db())
    return jsonify({"ok": True})


@bp.route("/shopping/clear_all", methods=["POST"])
def shopping_clear_all():
    get_db().execute("DELETE FROM shopping_items")
    get_db().commit()
    return jsonify({"ok": True})


# ── Alexa Shopping List ────────────────────────────────────────────────────────

@bp.route("/alexa/auth")
def alexa_auth():
    if "person" in request.args:
        session["person"] = request.args["person"]
    if current_person() not in config.ADMINS:
        return "Admin only — add ?person=paul or ?person=katie to the URL", 403
    if not config.ALEXA_CLIENT_ID:
        return "ALEXA_CLIENT_ID not set in .env — see .env.example", 400
    return redirect(alexa.get_auth_url())


@bp.route("/alexa/oauth2callback")
def alexa_oauth_callback():
    code = request.args.get("code")
    if not code:
        error = request.args.get("error", "unknown")
        log.warning("Alexa OAuth callback error: %s", error)
        return f"Alexa auth failed: {error}", 400
    ok = alexa.exchange_code(code, get_db())
    if ok:
        return redirect(url_for("shopping.meals_view"))
    return "Alexa auth failed — check server logs", 500


@bp.route("/shopping/sync_alexa", methods=["POST"])
@require_admin
def shopping_sync_alexa():
    if not config.ALEXA_CLIENT_ID:
        return jsonify({"error": "Alexa not configured"}), 400
    pushed = alexa.sync_shopping_list_to_alexa(get_db())
    return jsonify({"ok": True, "pushed": pushed})


@bp.route("/shopping/alexa_items")
def shopping_alexa_items():
    if not alexa.is_connected(get_db()):
        return jsonify([])
    return jsonify(alexa.get_alexa_shopping_items(get_db()))
