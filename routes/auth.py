"""Auth blueprint — login, logout, set_person, service worker, offline."""
from __future__ import annotations

import logging

from flask import (
    Blueprint, redirect, render_template, request, session, url_for,
)

import config
from modules import auth
from routes.utils import current_person, get_db

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)


def _google_redirect_uri() -> str:
    return config.APP_BASE_URL.rstrip("/") + "/login/google/callback"


@bp.route("/login")
def login():
    if session.get("authenticated"):
        return redirect(url_for("dashboard.dashboard"))
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


@bp.route("/login/pin", methods=["POST"])
def login_pin():
    person  = request.form.get("person", "").strip()
    pin_val = request.form.get("pin", "").strip()
    next_url = request.form.get("next", "") or url_for("dashboard.dashboard")

    if person not in config.PEOPLE + ["family"]:
        return redirect(url_for("auth.login", error="Invalid person", next=next_url))

    if person in auth.GOOGLE_LOGIN_PERSONS:
        return redirect(url_for("auth.login", error="Please use Google to sign in", next=next_url))

    db = get_db()

    if person == "family":
        row = db.execute("SELECT value FROM app_settings WHERE key='family_passcode'").fetchone()
        hashed = row["value"] if row else None
    else:
        row = db.execute("SELECT login_pin FROM person_prefs WHERE person=?", (person,)).fetchone()
        hashed = row["login_pin"] if row else None

    if not hashed or not auth.check_pin(pin_val, hashed):
        return redirect(url_for("auth.login", error="Incorrect PIN", next=next_url))

    session.permanent = True
    session["authenticated"] = True
    session["person"]      = person
    session["auth_person"] = person
    return redirect(next_url)


@bp.route("/login/google")
def login_google():
    url, state = auth.google_login_url(_google_redirect_uri())
    session["oauth_login_state"] = state
    return redirect(url)


@bp.route("/login/google/callback")
def login_google_callback():
    if request.args.get("state") != session.pop("oauth_login_state", None):
        return redirect(url_for("auth.login", error="Invalid state — please try again"))
    code = request.args.get("code")
    if not code:
        return redirect(url_for("auth.login", error="Google login cancelled"))
    email = auth.google_exchange_code(code, _google_redirect_uri())
    if not email:
        return redirect(url_for("auth.login", error="Could not retrieve email from Google"))
    person = config.GOOGLE_AUTHORIZED_EMAILS.get(email)
    if not person:
        return redirect(url_for("auth.login", error=f"Email not authorised: {email}"))
    session.permanent = True
    session["authenticated"] = True
    session["person"]      = person
    session["auth_person"] = person
    return redirect(url_for("dashboard.dashboard"))


@bp.route("/sw.js")
def service_worker():
    from flask import current_app
    return current_app.send_static_file("sw.js"), 200, {"Content-Type": "application/javascript"}


@bp.route("/offline")
def offline():
    return render_template("offline.html"), 200


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/set_person/<person>")
def set_person(person: str):
    if person in config.PEOPLE + ["family"]:
        session["person"] = person
    # Redirect back but strip ?person= so the route doesn't override the session
    referrer = request.referrer or url_for("dashboard.dashboard")
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    parsed = urlparse(referrer)
    qs = {k: v for k, v in parse_qs(parsed.query).items() if k != "person"}
    clean = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    return redirect(clean)
