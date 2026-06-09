"""Shared helpers used by all route blueprints."""
from __future__ import annotations

import sqlite3
import threading
import time as _time
from functools import wraps
from pathlib import Path

import database

from flask import g, jsonify, session

import config


# ── In-memory page/API response cache ─────────────────────────────────────────

_pcache: dict[str, tuple[float, object]] = {}
_pcache_lock = threading.Lock()


def _pcache_get(key: str, ttl: float):
    """Return cached payload if age < ttl seconds, else None."""
    with _pcache_lock:
        entry = _pcache.get(key)
    if not entry:
        return None
    ts, data = entry
    return data if (_time.time() - ts) < ttl else None


def _pcache_set(key: str, data):
    with _pcache_lock:
        _pcache[key] = (_time.time(), data)


def _pcache_bust(key: str):
    with _pcache_lock:
        _pcache.pop(key, None)


# ── Database helpers ──────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        if config.DB_DRIVER == "mysql":
            g.db = database.get_connection()
        else:
            db_path = Path(config.DB_PATH)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(str(db_path))
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def _get_db_for_thread():
    """Open a plain connection for background threads (no Flask context)."""
    if config.DB_DRIVER == "mysql":
        return database.get_connection()
    db_path = Path(config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ── Person helpers ─────────────────────────────────────────────────────────────

def current_person() -> str:
    return session.get("person", "family")


def auth_person() -> str:
    """The person who actually logged in — never changes with the view switcher."""
    return session.get("auth_person") or session.get("person", "family")


def get_prefs(db, person: str) -> dict:
    row = db.execute("SELECT * FROM person_prefs WHERE person=?", (person,)).fetchone()
    return dict(row) if row else {"completed_style": "fade"}


# ── Admin decorator ────────────────────────────────────────────────────────────

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        if current_person() not in config.ADMINS:
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated
