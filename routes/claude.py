"""Claude blueprint — /claude, a launcher page for the mobile-claude terminal app."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for

import config
from routes.utils import current_person, get_db, get_prefs

bp = Blueprint("claude", __name__)


@bp.route("/claude")
def claude_view():
    person = current_person()
    if person not in config.ADMINS:
        return redirect(url_for("settings.settings_view"))
    db = get_db()
    return render_template(
        "claude.html",
        person=person,
        prefs=get_prefs(db, person),
        is_admin=True,
        lan_url=config.CLAUDE_MOBILE_LAN_URL,
        tailscale_url=config.CLAUDE_MOBILE_TAILSCALE_URL,
    )
