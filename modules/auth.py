"""Authentication helpers — PIN hashing and Google OAuth login flow."""
from __future__ import annotations

import secrets
import requests
from urllib.parse import urlencode

import config

try:
    import bcrypt
    _BCRYPT = True
except ImportError:
    _BCRYPT = False

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_INFO_URL  = "https://www.googleapis.com/oauth2/v3/userinfo"

# Persons who can use Google OAuth login
GOOGLE_LOGIN_PERSONS = {"katie", "paul"}


def hash_pin(pin: str) -> str:
    if not _BCRYPT:
        raise RuntimeError("bcrypt is not installed — run: pip install bcrypt")
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()


def check_pin(pin: str, hashed: str) -> bool:
    if not _BCRYPT or not hashed:
        return False
    try:
        return bcrypt.checkpw(pin.encode(), hashed.encode())
    except Exception:
        return False


def google_login_url(redirect_uri: str) -> tuple[str, str]:
    """Return (url, state). Caller stores state in session for CSRF check."""
    state = secrets.token_urlsafe(16)
    params = {
        "client_id":     config.GOOGLE_CLIENT_ID,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "prompt":        "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}", state


def google_exchange_code(code: str, redirect_uri: str) -> str | None:
    """Exchange auth code → email address. Returns None on any failure."""
    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        }, timeout=10)
        token = resp.json()
        if "access_token" not in token:
            return None
        info = requests.get(
            GOOGLE_INFO_URL,
            headers={"Authorization": f"Bearer {token['access_token']}"},
            timeout=10,
        )
        return info.json().get("email", "").strip().lower() or None
    except Exception:
        return None
