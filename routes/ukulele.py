"""Ukulele Songbook — /ukulele (standalone music-stand web app)."""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, render_template, send_from_directory

bp = Blueprint("ukulele", __name__)

SONGS_ROOT = Path(__file__).resolve().parent.parent / "data" / "ukulele_songs"


@bp.route("/ukulele")
def ukulele_view():
    return render_template("ukulele.html")


@bp.route("/ukulele/songs.json")
def ukulele_songs_index():
    return send_from_directory(SONGS_ROOT, "songs.json")


@bp.route("/ukulele/songs/<path:filename>")
def ukulele_song(filename):
    if not filename.endswith(".json"):
        abort(404)
    return send_from_directory(SONGS_ROOT / "songs", filename)
