"""Web Push notifications via VAPID / pywebpush.

Keys are auto-generated on first use and stored in app_settings.
Override with VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY env vars.
"""
from __future__ import annotations
import base64
import json
import logging

import config

log = logging.getLogger(__name__)

_PRIV_KEY = "vapid_private_key"
_PUB_KEY  = "vapid_public_key"


def _generate_keys() -> tuple[str, str]:
    """Return (private_pem_pkcs8, public_key_b64url).

    pywebpush v2 (py_vapid Vapid02) requires PKCS8 PEM format:
    '-----BEGIN PRIVATE KEY-----'  (NOT '-----BEGIN EC PRIVATE KEY-----')
    """
    from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )
    key     = generate_private_key(SECP256R1())
    # PKCS8 — the only format pywebpush v2 can deserialise
    priv    = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    pub_b64 = base64.urlsafe_b64encode(
        key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    ).rstrip(b"=").decode()
    return priv, pub_b64


def _get_or_create_keys(db_conn) -> tuple[str, str]:
    """Return VAPID keys, generating and persisting them if needed."""
    if config.VAPID_PRIVATE_KEY and config.VAPID_PUBLIC_KEY:
        return config.VAPID_PRIVATE_KEY, config.VAPID_PUBLIC_KEY

    rows = {r["key"]: r["value"] for r in db_conn.execute(
        "SELECT key, value FROM app_settings WHERE key IN (?,?)", (_PRIV_KEY, _PUB_KEY)
    ).fetchall()}

    if _PRIV_KEY in rows and _PUB_KEY in rows:
        return rows[_PRIV_KEY], rows[_PUB_KEY]

    # Generate new keys
    priv, pub = _generate_keys()
    for k, v in ((_PRIV_KEY, priv), (_PUB_KEY, pub)):
        existing = db_conn.execute("SELECT key FROM app_settings WHERE key=?", (k,)).fetchone()
        if existing:
            db_conn.execute("UPDATE app_settings SET value=? WHERE key=?", (v, k))
        else:
            db_conn.execute("INSERT INTO app_settings (key, value) VALUES (?,?)", (k, v))
    db_conn.commit()
    log.info("VAPID keys auto-generated and stored in DB")
    return priv, pub


def get_public_key(db_conn) -> str:
    _, pub = _get_or_create_keys(db_conn)
    return pub


def send_push(subscription: dict, title: str, body: str, url: str, db_conn,
              urgency: str = "default") -> bool:
    """Send a Web Push to a single subscription dict {endpoint, p256dh, auth}.

    `urgency` (low/default/high/critical) is passed through in the payload so
    the service worker can pick a vibration pattern and tell open tabs which
    sound to play — see static/sw.js and the message listener in base.html.

    Automatically removes the subscription from the DB if the push service
    returns 404/410 (expired or unregistered subscription).
    """
    try:
        priv, _ = _get_or_create_keys(db_conn)
        from pywebpush import webpush, WebPushException
        from py_vapid import Vapid02
        # pywebpush's webpush() treats a raw string as base64url via from_string(),
        # which chokes on PKCS8 PEM headers. Build the Vapid02 object explicitly
        # using from_pem() so pywebpush sees an already-parsed Vapid01 instance.
        vapid = Vapid02.from_pem(priv.encode())
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps({"title": title, "body": body, "url": url, "urgency": urgency}),
            vapid_private_key=vapid,
            vapid_claims={"sub": config.VAPID_SUBJECT},
        )
        return True
    except Exception as exc:
        endpoint = subscription.get("endpoint", "")
        # 404/410 = subscription is gone on the push service side; clean up DB
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            log.info("Push subscription expired (HTTP %s) — removing: %.60s", status, endpoint)
            try:
                db_conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
                db_conn.commit()
            except Exception:
                pass
        else:
            log.warning("Web Push failed (%.50s): %s", endpoint, exc)
        return False


def send_push_to_person(db_conn, person: str, title: str, body: str, url: str = None,
                        urgency: str = "default") -> int:
    """Send to all subscriptions for a person. Returns count sent."""
    rows = db_conn.execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE person=?", (person,)
    ).fetchall()
    dest = url or config.APP_BASE_URL
    return sum(send_push(dict(r), title, body, dest, db_conn, urgency=urgency) for r in rows)
