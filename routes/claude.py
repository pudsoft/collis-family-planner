"""Claude blueprint — /claude, a launcher page for the mobile-claude terminal app."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for

import config
from routes.utils import current_person

bp = Blueprint("claude", __name__)


@bp.route("/claude")
def claude_view():
    person = current_person()
    if person not in config.ADMINS:
        return redirect(url_for("settings.settings_view"))
    return render_template(
        "claude.html",
        person=person,
        is_admin=True,
        lan_url=config.CLAUDE_MOBILE_LAN_URL,
        tailscale_url=config.CLAUDE_MOBILE_TAILSCALE_URL,
    )
