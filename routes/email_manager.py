"""Email manager blueprint — /email routes."""
from __future__ import annotations

import logging
import re

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

import config
from modules import email_accounts as accounts_mod
from modules import imap_mail
from routes.utils import auth_person, get_db, get_prefs

log = logging.getLogger(__name__)
bp = Blueprint("email_manager", __name__)


def _email_enabled(db) -> bool:
    row = db.execute("SELECT value FROM app_settings WHERE key='email_enabled'").fetchone()
    return not (row and row["value"] == "0")


@bp.route("/email")
def email_view():
    person   = auth_person()
    db       = get_db()
    if not _email_enabled(db):
        return redirect(url_for("dashboard.home_grid"))
    accounts = accounts_mod.list_accounts(db, person)
    return render_template(
        "email_manager.html",
        accounts=accounts,
        person=person,
        prefs=get_prefs(db, person),
        is_admin=person in config.ADMINS,
        people=config.PEOPLE,
        person_display=config.PERSON_DISPLAY,
    )


@bp.route("/email/<int:account_id>/messages")
def email_messages(account_id: int):
    person = auth_person()
    creds  = accounts_mod.get_credentials(get_db(), account_id, person)
    if not creds:
        return jsonify({"error": "Account not found"}), 404

    mailbox     = request.args.get("mailbox", "INBOX")
    limit       = min(int(request.args.get("limit", 200)), 500)
    unread_only = request.args.get("unread_only", "1") == "1"
    since_days  = int(request.args.get("since_days", 90))
    try:
        msgs = imap_mail.list_messages(creds["email"], creds["password"],
                                       mailbox=mailbox, limit=limit,
                                       unread_only=unread_only,
                                       since_days=since_days)
        return jsonify(msgs)
    except Exception as exc:
        log.exception("IMAP fetch failed for account %s", account_id)
        return jsonify({"error": str(exc)}), 500


@bp.route("/email/<int:account_id>/delete", methods=["POST"])
def email_delete(account_id: int):
    person = auth_person()
    creds  = accounts_mod.get_credentials(get_db(), account_id, person)
    if not creds:
        return jsonify({"error": "Account not found"}), 404

    body    = request.get_json(force=True) or {}
    uids    = body.get("uids", [])
    mailbox = body.get("mailbox", "INBOX")
    if not uids:
        return jsonify({"error": "No UIDs provided"}), 400
    try:
        count = imap_mail.delete_messages(creds["email"], creds["password"],
                                          [str(u) for u in uids], mailbox=mailbox)
        return jsonify({"ok": True, "deleted": count})
    except Exception as exc:
        log.exception("IMAP delete failed")
        return jsonify({"error": str(exc)}), 500


@bp.route("/email/<int:account_id>/unsubscribe", methods=["POST"])
def email_unsubscribe(account_id: int):
    person = auth_person()
    if not accounts_mod.get_credentials(get_db(), account_id, person):
        return jsonify({"error": "Account not found"}), 404

    body   = request.get_json(force=True) or {}
    header = body.get("list_unsubscribe", "")

    urls        = re.findall(r"<([^>]+)>", header)
    http_urls   = [u for u in urls if u.lower().startswith("http")]
    mailto_url  = next((u for u in urls if u.lower().startswith("mailto:")), None)

    if http_urls:
        return jsonify(imap_mail.http_unsubscribe(http_urls[0]))
    if mailto_url:
        return jsonify({"ok": None, "mailto": mailto_url,
                        "note": "Manual email required to unsubscribe"})
    return jsonify({"ok": False, "error": "No unsubscribe URL found in header"})
