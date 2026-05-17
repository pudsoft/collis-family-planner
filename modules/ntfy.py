"""NTFY push notifications — ported from the Rythm project app.py."""
from __future__ import annotations

import logging
import requests
from config import NTFY_BASE_URL, APP_BASE_URL

log = logging.getLogger(__name__)


def send_ntfy(channel: str, message: str, title: str = None,
              click_url: str = None, priority: str = "default") -> bool:
    if not channel:
        log.warning("send_ntfy called with no channel — skipping")
        return False
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if title:
        headers["Title"] = title
    if click_url:
        headers["Click"] = click_url
    if priority:
        headers["Priority"] = priority
    try:
        requests.post(
            f"{NTFY_BASE_URL}/{channel}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=5,
        )
        return True
    except Exception as e:
        log.warning("NTFY send failed (channel=%s): %s", channel, e)
        return False


def send_task_reminder(channel: str, person: str, task_id: int,
                       task_title: str, priority: str = "default") -> bool:
    url = f"{APP_BASE_URL}/tasks?person={person}&task={task_id}"
    return send_ntfy(
        channel=channel,
        message=f"Task due: {task_title}",
        title="📋 Task Reminder",
        click_url=url,
        priority=priority,
    )


def send_event_reminder(channel: str, person: str, event_id: str,
                        event_title: str, priority: str = "default") -> bool:
    url = f"{APP_BASE_URL}/calendar?person={person}&event={event_id}"
    return send_ntfy(
        channel=channel,
        message=f"Coming up: {event_title}",
        title="📅 Event Reminder",
        click_url=url,
        priority=priority,
    )


def send_medicine_reminder(channel: str, person: str,
                           medicine_name: str, priority: str = "high") -> bool:
    url = f"{APP_BASE_URL}/medicines?person={person}"
    return send_ntfy(
        channel=channel,
        message=f"Time to take: {medicine_name}",
        title="💊 Medicine Reminder",
        click_url=url,
        priority=priority,
    )


def send_reorder_alert(channel: str, person: str,
                       medicine_name: str, days_left: int) -> bool:
    url = f"{APP_BASE_URL}/medicines?person={person}"
    return send_ntfy(
        channel=channel,
        message=f"{medicine_name} — approximately {days_left} days of stock remaining. Time to reorder!",
        title="⚠️ Medicine Reorder Needed",
        click_url=url,
        priority="high",
    )


def send_broadcast(channels: list[str], message: str,
                   title: str = "📢 Family Notice", priority: str = "default") -> int:
    """Send the same message to multiple channels. Returns count of successes."""
    return sum(send_ntfy(c, message, title=title, priority=priority) for c in channels if c)
