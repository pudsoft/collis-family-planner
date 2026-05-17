"""Amazon Alexa Shopping List integration via Login with Alexa (LWA) OAuth2."""
from __future__ import annotations

import logging
import json
import requests
from datetime import datetime, timezone

from config import ALEXA_CLIENT_ID, ALEXA_CLIENT_SECRET, APP_BASE_URL

log = logging.getLogger(__name__)

# Amazon OAuth endpoints
_AUTH_URL    = "https://www.amazon.com/ap/oa"
_TOKEN_URL   = "https://api.amazon.com/auth/o2/token"
_LIST_API    = "https://api.amazonalexa.com/v2/householdlists"

# Scopes needed for read + write access to Alexa household lists
_SCOPES = "alexa::household:lists:read alexa::household:lists:write"

REDIRECT_URI = f"{APP_BASE_URL}/alexa/oauth2callback"


# ── OAuth helpers ─────────────────────────────────────────────────────────────

def get_auth_url() -> str:
    """Return the Amazon LWA authorisation URL."""
    from urllib.parse import urlencode
    params = {
        "client_id":     ALEXA_CLIENT_ID,
        "scope":         _SCOPES,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, db_conn) -> bool:
    """Exchange authorisation code for access + refresh tokens; store in DB."""
    try:
        resp = requests.post(_TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "client_id":     ALEXA_CLIENT_ID,
            "client_secret": ALEXA_CLIENT_SECRET,
        }, timeout=10)
        resp.raise_for_status()
        token = resp.json()
        token["obtained_at"] = datetime.now(timezone.utc).isoformat()
        _save_token(db_conn, token)
        log.info("Alexa OAuth complete — token stored")
        return True
    except Exception as e:
        log.error("Alexa OAuth exchange failed: %s", e)
        return False


def _save_token(db_conn, token: dict):
    db_conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('alexa_token', ?)",
        (json.dumps(token),)
    )
    db_conn.commit()


def _load_token(db_conn) -> dict | None:
    row = db_conn.execute(
        "SELECT value FROM app_settings WHERE key='alexa_token'"
    ).fetchone()
    return json.loads(row["value"]) if row else None


def _refresh_token(db_conn, token: dict) -> dict | None:
    """Use refresh_token to get a new access_token; persist and return updated token."""
    try:
        resp = requests.post(_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id":     ALEXA_CLIENT_ID,
            "client_secret": ALEXA_CLIENT_SECRET,
        }, timeout=10)
        resp.raise_for_status()
        new_token = resp.json()
        new_token["refresh_token"] = token["refresh_token"]  # preserve refresh token
        new_token["obtained_at"] = datetime.now(timezone.utc).isoformat()
        _save_token(db_conn, new_token)
        log.info("Alexa token refreshed")
        return new_token
    except Exception as e:
        log.warning("Alexa token refresh failed: %s", e)
        return None


def _get_valid_token(db_conn) -> dict | None:
    """Return a valid (refreshed if needed) token dict, or None if not connected."""
    token = _load_token(db_conn)
    if not token:
        return None
    # Refresh if within 5 minutes of expiry (expires_in is in seconds)
    obtained = datetime.fromisoformat(token.get("obtained_at", "2000-01-01T00:00:00+00:00"))
    expires_in = token.get("expires_in", 3600)
    age = (datetime.now(timezone.utc) - obtained).total_seconds()
    if age >= expires_in - 300:
        token = _refresh_token(db_conn, token)
    return token


def is_connected(db_conn) -> bool:
    return _load_token(db_conn) is not None


# ── List API ──────────────────────────────────────────────────────────────────

def _headers(token: dict) -> dict:
    return {
        "Authorization": f"Bearer {token['access_token']}",
        "Content-Type":  "application/json",
    }


def _get_shopping_list_id(token: dict) -> str | None:
    """Return the listId for the default Alexa Shopping List."""
    try:
        resp = requests.get(f"{_LIST_API}/", headers=_headers(token), timeout=10)
        resp.raise_for_status()
        for lst in resp.json().get("lists", []):
            if lst.get("name") == "Alexa shopping list":
                return lst["listId"]
        log.warning("Alexa shopping list not found in household lists")
        return None
    except Exception as e:
        log.warning("Could not fetch Alexa lists: %s", e)
        return None


def get_alexa_shopping_items(db_conn) -> list[dict]:
    """Fetch active items from the Alexa Shopping List."""
    token = _get_valid_token(db_conn)
    if not token:
        return []
    list_id = _get_shopping_list_id(token)
    if not list_id:
        return []
    try:
        resp = requests.get(
            f"{_LIST_API}/{list_id}/active",
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {"id": item["id"], "value": item["value"], "status": item["status"]}
            for item in resp.json().get("items", [])
        ]
    except Exception as e:
        log.warning("Could not fetch Alexa shopping items: %s", e)
        return []


def add_to_alexa_list(db_conn, item_value: str) -> bool:
    """Add a single item to the Alexa Shopping List."""
    token = _get_valid_token(db_conn)
    if not token:
        log.warning("add_to_alexa_list: not connected")
        return False
    list_id = _get_shopping_list_id(token)
    if not list_id:
        return False
    try:
        resp = requests.post(
            f"{_LIST_API}/{list_id}/items",
            headers=_headers(token),
            json={"value": item_value, "status": "active"},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Added to Alexa list: %s", item_value)
        return True
    except Exception as e:
        log.warning("Could not add '%s' to Alexa list: %s", item_value, e)
        return False


def sync_shopping_list_to_alexa(db_conn) -> int:
    """Push all unchecked local shopping items to Alexa. Returns count pushed."""
    token = _get_valid_token(db_conn)
    if not token:
        return 0
    list_id = _get_shopping_list_id(token)
    if not list_id:
        return 0

    # Get current Alexa items to avoid duplicates (case-insensitive)
    existing = {i["value"].lower() for i in get_alexa_shopping_items(db_conn)}

    local_items = db_conn.execute(
        "SELECT item, quantity FROM shopping_items WHERE checked=0 ORDER BY category, item"
    ).fetchall()

    pushed = 0
    for row in local_items:
        label = f"{row['item']} ({row['quantity']})" if row["quantity"] else row["item"]
        if label.lower() not in existing:
            if add_to_alexa_list(db_conn, label):
                pushed += 1

    log.info("Synced %d items to Alexa shopping list", pushed)
    return pushed
