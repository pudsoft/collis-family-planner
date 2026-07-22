"""In-app notification feed — persisted log of pushes sent to family members.

Every notification triggered via the app (medicine/birthday/MOT reminders,
task transfers, or the external /api/notify endpoint) is written here so it
shows up in each person's /notifications feed, in addition to being sent as
a Web Push if they have a subscription registered.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from modules import push_notif

log = logging.getLogger(__name__)


def create_notification(db, person: str, title: str, body: str = "", url: str = None,
                         urgency: str = "default", send_push: bool = True) -> int:
    """Log a notification for `person` (or 'family' to target everyone) and
    optionally deliver it as a Web Push. Returns the new notification id.

    `url` is stored as given and drives the card's "Open" button — leave it
    unset for a plain FYI notification. Either way, the push notification
    itself always deep-links into /notifications?notif=<id> so tapping it
    (when no more specific `url` was given) lands on and highlights the
    matching card instead of just opening the app root.
    """
    if urgency not in config.NOTIFY_URGENCY_LEVELS:
        urgency = "default"

    created_at = datetime.now(timezone.utc).isoformat()
    cur = db.execute(
        "INSERT INTO notifications (person, title, body, url, urgency, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (person, title, body, url, urgency, created_at),
    )
    db.commit()
    notif_id = cur.lastrowid

    push_url = url or f"{config.APP_BASE_URL.rstrip('/')}/notifications?notif={notif_id}"
    if send_push:
        targets = config.PEOPLE if person == "family" else [person]
        for p in targets:
            try:
                push_notif.send_push_to_person(db, p, title, body, push_url, urgency=urgency)
            except Exception:
                log.exception("Failed to push notification %s to %s", notif_id, p)

    return notif_id


def get_notifications(db, person: str) -> list[dict]:
    """Notifications visible to `person`: their own plus any family-wide ones."""
    rows = db.execute(
        "SELECT * FROM notifications WHERE person=? OR person='family' "
        "ORDER BY created_at DESC",
        (person,),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_notification(db, notif_id: int, person: str) -> bool:
    """Delete a notification, scoped to what `person` is allowed to see."""
    cur = db.execute(
        "DELETE FROM notifications WHERE id=? AND (person=? OR person='family')",
        (notif_id, person),
    )
    db.commit()
    return cur.rowcount > 0


def clear_all_notifications(db, person: str) -> int:
    """Delete every notification visible to `person` (their own plus
    family-wide ones). Returns the number of rows deleted."""
    cur = db.execute(
        "DELETE FROM notifications WHERE person=? OR person='family'",
        (person,),
    )
    db.commit()
    return cur.rowcount
