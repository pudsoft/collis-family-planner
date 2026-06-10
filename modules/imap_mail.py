"""IMAP email operations — headers only, never fetches body content."""
from __future__ import annotations

import email as _email_module
import imaplib
import logging
import re
from email.header import decode_header as _decode_header

import requests

log = logging.getLogger(__name__)

_IMAP_HOST = "imap.gmail.com"
_IMAP_PORT = 993
_TIMEOUT   = 30
_FIELDS    = "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE MESSAGE-ID)])"


def _connect(email_address: str, password: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
    conn.socket().settimeout(_TIMEOUT)
    conn.login(email_address, password)
    return conn


def _decode_str(raw: str | None) -> str:
    if not raw:
        return ""
    parts = _decode_header(raw)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(str(chunk))
    return " ".join(out).strip()


def list_messages(email_address: str, password: str,
                  mailbox: str = "INBOX", limit: int = 200) -> list[dict]:
    """Fetch email headers only. Never fetches body content."""
    conn = _connect(email_address, password)
    msgs: list[dict] = []
    try:
        status, _ = conn.select(f'"{mailbox}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"Cannot select mailbox '{mailbox}'")

        _, data = conn.uid("SEARCH", None, "ALL")
        all_uids = (data[0] or b"").split()
        uids = all_uids[-limit:]
        if not uids:
            return []

        for i in range(0, len(uids), 50):
            batch = b",".join(uids[i:i + 50])
            _, items = conn.uid("FETCH", batch, _FIELDS)
            if not items:
                continue
            for item in items:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                meta    = item[0].decode("ascii", errors="replace")
                uid_m   = re.search(r"UID (\d+)", meta)
                flags_m = re.search(r"FLAGS \(([^)]*)\)", meta)
                if not uid_m:
                    continue
                uid   = uid_m.group(1)
                flags = flags_m.group(1) if flags_m else ""
                seen  = "\\Seen" in flags

                msg = _email_module.message_from_bytes(
                    item[1] if isinstance(item[1], bytes) else b""
                )
                msgs.append({
                    "uid":              uid,
                    "from":             _decode_str(msg.get("From", "")),
                    "subject":          _decode_str(msg.get("Subject", "")),
                    "date":             msg.get("Date", ""),
                    "list_unsubscribe": msg.get("List-Unsubscribe", ""),
                    "message_id":       msg.get("Message-ID", ""),
                    "seen":             seen,
                })
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    msgs.reverse()  # newest first
    return msgs


def delete_messages(email_address: str, password: str,
                    uids: list[str], mailbox: str = "INBOX") -> int:
    """Move messages to Gmail Trash. Returns number moved."""
    if not uids:
        return 0
    conn = _connect(email_address, password)
    moved = 0
    try:
        conn.select(f'"{mailbox}"')
        for uid in uids:
            uid_b = uid.encode() if isinstance(uid, str) else uid
            ok, _ = conn.uid("COPY", uid_b, '"[Gmail]/Trash"')
            if ok == "OK":
                conn.uid("STORE", uid_b, "+FLAGS", "\\Deleted")
                moved += 1
        conn.expunge()
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return moved


def http_unsubscribe(url: str) -> dict:
    """Attempt HTTP GET unsubscribe. Returns result dict."""
    try:
        resp = requests.get(
            url, timeout=15, allow_redirects=True,
            headers={"User-Agent": "CollisFamilyPlanner/1.0"},
        )
        return {"ok": resp.ok, "status": resp.status_code, "url": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}
