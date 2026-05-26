#!/usr/bin/env python3
"""
temperature_logger.py
=====================
Polls outdoor temperature for Brundall, Norfolk, UK (Open-Meteo — no API key
needed) and every Hive radiator zone's current temperature and heating state,
then appends all readings to a local SQLite database.

Intended to run every 15 minutes via cron on the OCI server:

    */15 * * * * cd /home/ubuntu/collis-family-planner && \
      /usr/bin/doppler run -- \
      /home/ubuntu/collis-family-planner/venv/bin/python \
      scripts/temperature_logger.py \
      >> /home/ubuntu/collis-family-planner/logs/temp_logger.log 2>&1
"""
from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import sys

import requests

# ── Path setup ────────────────────────────────────────────────────────────────
# Add the app root so we can import config and modules.hive
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import config                            # reads Doppler env vars via dotenv
from modules.hive import get_climate_data

# ── Constants ─────────────────────────────────────────────────────────────────
DB_PATH      = os.path.join(APP_DIR, "data", "temperature_log.db")
BRUNDALL_LAT = 52.617
BRUNDALL_LON = 1.467

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS temperature_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT    NOT NULL,
            source      TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            temperature REAL,
            target_temp REAL,
            state       TEXT,
            is_heating  INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_recorded_at ON temperature_log (recorded_at)
    """)
    conn.commit()
    return conn


# ── Data fetchers ─────────────────────────────────────────────────────────────

def fetch_outdoor_temp() -> float:
    """Current 2 m air temperature for Brundall via Open-Meteo (free, no key)."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={BRUNDALL_LAT}&longitude={BRUNDALL_LON}"
        "&current=temperature_2m&forecast_days=1"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["current"]["temperature_2m"])


def fetch_hive_rows() -> list[tuple]:
    """Return Hive zone readings as row tuples ready for INSERT."""
    zones = get_climate_data()
    rows: list[tuple] = []
    for z in zones:
        is_heating = z.get("is_heating", False)
        mode       = z.get("mode", "SCHEDULE")
        if mode == "OFF":
            state = "off"
        elif is_heating:
            state = "heating"
        else:
            state = "scheduled"

        rows.append((
            z["name"],
            z.get("current_temp"),
            z.get("target_temp"),
            state,
            1 if is_heating else 0,
        ))
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    now  = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_db()
    rows: list[tuple] = []

    # ── Outdoor temperature ──────────────────────────────────────────────────
    try:
        temp = fetch_outdoor_temp()
        rows.append((now, "outdoor", "Brundall", temp, None, None, None))
        log.info("Outdoor  Brundall               %.1f °C", temp)
    except Exception as exc:
        log.warning("Outdoor fetch failed: %s", exc)

    # ── Hive radiators ───────────────────────────────────────────────────────
    if config.HIVE_EMAIL and config.HIVE_PASSWORD:
        try:
            for name, current, target, state, heating in fetch_hive_rows():
                rows.append((now, "hive", name, current, target, state, heating))
                log.info(
                    "Hive     %-24s  %.1f °C  → %.0f °C  [%s]",
                    name, current or 0.0, target or 0.0, state,
                )
        except Exception as exc:
            log.warning("Hive fetch failed: %s", exc)
    else:
        log.warning("HIVE_EMAIL / HIVE_PASSWORD not set — skipping Hive")

    # ── Persist ──────────────────────────────────────────────────────────────
    if rows:
        conn.executemany(
            "INSERT INTO temperature_log"
            " (recorded_at, source, name, temperature, target_temp, state, is_heating)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        log.info("Wrote %d reading(s) to %s", len(rows), DB_PATH)
    else:
        log.warning("No readings collected — nothing written")

    conn.close()


if __name__ == "__main__":
    main()
