"""Hive smart heating — TRV temperatures, zone states, boiler status.

Uses pyhiveapi for Cognito SRP authentication (required by Hive), then
queries the Beekeeper REST API for device data. Results are cached for
60 seconds to avoid hammering the API on every page poll.
"""
from __future__ import annotations

import asyncio
import logging
import time
import config

log = logging.getLogger(__name__)

_CACHE_TTL = 60   # seconds

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache_ts:   float      = 0.0
_cache_data: list[dict] = []


# ── Async runner (Flask is sync) ──────────────────────────────────────────────

def _run(coro):
    """Run an async coroutine from a synchronous Flask request context."""
    try:
        # asyncio.run() creates a fresh event loop — safe in sync Flask
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


# ── Hive auth + data fetch ────────────────────────────────────────────────────

async def _fetch_products() -> list[dict]:
    """Authenticate with Hive and return all products from the API."""
    from pyhiveapi import Hive, HiveAuth, SMS_REQUIRED

    hive   = Hive()
    auth   = HiveAuth(config.HIVE_EMAIL, config.HIVE_PASSWORD)
    result = await hive.session.startSession(auth)

    if result == SMS_REQUIRED:
        raise RuntimeError(
            "Hive requires SMS 2FA — please disable it temporarily or "
            "whitelist the server IP in your Hive account settings."
        )

    products = await hive.session.getProducts()
    return products or []


# ── Public API ────────────────────────────────────────────────────────────────

def get_climate_data() -> list[dict]:
    """
    Return all heating zones (boiler zones + TRVs) with temperature data.

    Each dict:
        id            – Hive product ID
        name          – zone/TRV name
        type          – "heating" | "trvcontrol"
        current_temp  – float °C (or None)
        target_temp   – float °C (or None)
        mode          – "SCHEDULE" | "MANUAL" | "OFF" | "BOOST"
        is_heating    – True if boiler is actively firing for this zone
        online        – bool
    """
    if not config.HIVE_EMAIL or not config.HIVE_PASSWORD:
        return []

    now = time.time()
    if now - _cache_ts < _CACHE_TTL:
        return _cache_data

    try:
        products = _run(_fetch_products())
        zones: list[dict] = []

        for p in products:
            ptype = p.get("type", "")
            if ptype not in ("heating", "trvcontrol"):
                continue

            props = p.get("props", {})
            state = p.get("state", {})

            current = props.get("temperature")
            target  = state.get("target") or state.get("heat")

            # Convert strings to float if needed
            try:
                current = float(current) if current is not None else None
            except (TypeError, ValueError):
                current = None
            try:
                target  = float(target)  if target  is not None else None
            except (TypeError, ValueError):
                target  = None

            zones.append({
                "id":           p.get("id", ""),
                "name":         state.get("name", "Heating"),
                "type":         ptype,
                "current_temp": current,
                "target_temp":  target,
                "mode":         state.get("mode", "SCHEDULE"),
                "is_heating":   bool(props.get("working", False)),
                "online":       bool(props.get("online", True)),
            })

        global _cache_ts, _cache_data
        _cache_data = zones
        _cache_ts   = now
        log.info("Hive: fetched %d zones", len(zones))
        return zones

    except Exception as exc:
        log.warning("Hive get_climate_data failed: %s", exc)
        # Return stale cache rather than empty on transient errors
        return _cache_data


def get_zone_by_id(zone_id: str) -> dict | None:
    """Look up a single zone from cached data."""
    return next(
        (z for z in get_climate_data() if z["id"] == zone_id),
        None,
    )
