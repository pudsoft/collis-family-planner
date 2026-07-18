#!/usr/bin/env python3
"""CLI helper to trigger a Family Planner push notification via /api/notify.

Examples:
    ./scripts/notify.py --person katie --title "Bin day" --body "Put the bins out"
    ./scripts/notify.py --person paul --person katie --title "Leak!" \\
        --body "Water under the sink" --urgency critical
    ./scripts/notify.py --person family --title "Dinner's ready" --urgency high

Urgency levels (low/default/high/critical) control the vibration pattern and
requireInteraction on the device, and — when the app is open in a tab (e.g.
the wall-mounted kiosk tablet) — which sound file plays
(static/sounds/<urgency>.wav). Plain OS push notifications can't carry a
custom sound in any current browser, so vibration/urgency is the reliable
signal when the app isn't already open.

Base URL and API key are read from this project's config.py (which loads
.env) by default. Override with --base-url / --key or the
NOTIFY_BASE_URL / NOTIFY_API_KEY env vars when running against a different
deployment (e.g. from a machine without this repo's .env).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

URGENCY_LEVELS = ("low", "default", "high", "critical")


def send(base_url: str, api_key: str, person: str, title: str, body: str,
         url: str | None, urgency: str) -> bool:
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/notify",
        headers={"X-API-Key": api_key},
        json={"person": person, "title": title, "body": body, "url": url, "urgency": urgency},
        timeout=10,
    )
    if resp.ok:
        print(f"✓ sent to {person} (id={resp.json().get('id')})")
        return True
    print(f"✗ failed for {person}: {resp.status_code} {resp.text}", file=sys.stderr)
    return False


def main():
    parser = argparse.ArgumentParser(description="Trigger a Family Planner push notification")
    parser.add_argument("--person", action="append", required=True,
                        help="Recipient: katie/paul/joshua/violet/family. Repeatable.")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--url", default=None, help="Deep-link opened when the notification is tapped")
    parser.add_argument("--urgency", default="default", choices=URGENCY_LEVELS)
    parser.add_argument("--base-url", default=os.getenv("NOTIFY_BASE_URL", config.APP_BASE_URL))
    parser.add_argument("--key", default=os.getenv("NOTIFY_API_KEY", config.NOTIFY_API_KEY))
    args = parser.parse_args()

    ok = True
    for person in args.person:
        ok = send(args.base_url, args.key, person.strip().lower(),
                  args.title, args.body, args.url, args.urgency) and ok

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
