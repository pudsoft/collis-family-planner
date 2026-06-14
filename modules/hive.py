"""Hive smart heating — TRV zone states and boiler status.

Authenticates via Cognito SRP (pyhiveapi), then queries the Beekeeper REST API
directly. We bypass pyhiveapi's startSession() because it crashes when the API
returns an empty homes list (IndexError not caught by the library).
"""
from __future__ import annotations

import logging
import time
import config

log = logging.getLogger(__name__)

_CACHE_TTL = 60   # seconds

_cache_ts:   float      = 0.0
_cache_data: list[dict] = []


def _fetch_all() -> tuple[list, list]:
    """Authenticate and return (products, devices) from Beekeeper API."""
    from pyhiveapi import Hive

    h = Hive(username=config.HIVE_EMAIL, password=config.HIVE_PASSWORD)

    login_result = h.login()

    if not login_result:
        raise RuntimeError("Hive login returned no result — check credentials")

    if "ChallengeName" in login_result and "AuthenticationResult" not in login_result:
        challenge = login_result["ChallengeName"]
        if challenge != "PASSWORD_VERIFIER":
            raise RuntimeError(
                f"Hive requires 2FA ({challenge}) — disable SMS 2FA in your Hive account"
            )

    if "AuthenticationResult" not in login_result:
        raise RuntimeError(f"Hive login failed: {login_result}")

    h.updateTokens(login_result, False)

    resp = h.api.getAll()
    status = str(resp.get("original", ""))
    if "20" not in status:
        raise RuntimeError(f"Beekeeper API error: {status}")

    parsed = resp.get("parsed") or {}
    return list(parsed.get("products", [])), list(parsed.get("devices", []))


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return round(f, 1) if f != 0.0 else None
    except (TypeError, ValueError):
        return None


def get_climate_data() -> list[dict]:
    """
    Return all heating zones (boiler zones + TRVs) with available data.

    Each dict:
        id            – Hive product ID
        name          – zone/TRV name
        type          – "heating" | "trvcontrol"
        current_temp  – float °C, or None if hub is offline
        target_temp   – float °C, or None
        mode          – "SCHEDULE" | "MANUAL" | "OFF" | "BOOST"
        is_heating    – True if boiler is actively firing for this zone
        online        – bool
    """
    global _cache_ts, _cache_data

    if not config.HIVE_EMAIL or not config.HIVE_PASSWORD:
        return []

    now = time.time()
    if now - _cache_ts < _CACHE_TTL:
        return _cache_data

    try:
        products, devices = _fetch_all()
        zones: list[dict] = []

        # Build device-id → full device dict for temperature fallback lookups
        dev_map: dict[str, dict] = {}
        for d in devices:
            did = d.get("id") or d.get("deviceId") or d.get("device_id")
            if did:
                dev_map[did] = d

        for p in products:
            ptype = p.get("type", "")
            if ptype not in ("heating", "trvcontrol"):
                continue

            props = p.get("props", {})
            state = p.get("state", {})

            # ── Temperature ──────────────────────────────────────────────────
            # Try every known location in order of likelihood.
            # As of Jun 2026 Hive removed temp from all reachable endpoints,
            # so current_temp will be None until they restore it.
            current = _safe_float(props.get("temperature"))

            if current is None:
                # Nested TRVs within the zone product
                for t in props.get("trvs", []):
                    if isinstance(t, dict):
                        v = (_safe_float(t.get("props", {}).get("temperature")) or
                             _safe_float(t.get("state", {}).get("temperature")))
                    else:
                        dev_t = dev_map.get(str(t), {})
                        v = (_safe_float(dev_t.get("props", {}).get("temperature")) or
                             _safe_float(dev_t.get("state", {}).get("temperature")))
                    if v is not None:
                        current = v
                        break

            if current is None:
                # Consumer devices listed in the zone
                for c in props.get("consumers", []):
                    if isinstance(c, dict):
                        v = (_safe_float(c.get("props", {}).get("temperature")) or
                             _safe_float(c.get("state", {}).get("temperature")))
                    else:
                        dev_c = dev_map.get(str(c), {})
                        v = (_safe_float(dev_c.get("props", {}).get("temperature")) or
                             _safe_float(dev_c.get("state", {}).get("temperature")))
                    if v is not None:
                        current = v
                        break

            if current is None:
                # Device with same ID as the product
                dev = dev_map.get(p.get("id", ""), {})
                current = (_safe_float(dev.get("props", {}).get("temperature")) or
                           _safe_float(dev.get("state", {}).get("temperature")))

            # ── Target temperature ───────────────────────────────────────────
            target = _safe_float(state.get("target")) or _safe_float(state.get("heat"))
            if target is None:
                schedule = state.get("schedule", {})
                if isinstance(schedule, dict):
                    slot = schedule.get("current") or {}
                    if not slot and "slots" in schedule:
                        slots = schedule.get("slots") or []
                        slot = slots[0] if slots else {}
                    target = (_safe_float(slot.get("target")) or
                              _safe_float(slot.get("heat")) or
                              _safe_float(slot.get("value")))

            # ── Mode ─────────────────────────────────────────────────────────
            mode = state.get("mode") or ("OFF" if state.get("frostProtection") else "SCHEDULE")

            zones.append({
                "id":           p.get("id", ""),
                "name":         state.get("name", props.get("zoneName", "Heating")),
                "type":         ptype,
                "current_temp": current,
                "target_temp":  target,
                "mode":         mode,
                "is_heating":   bool(props.get("working", False)),
                "online":       bool(props.get("online", True)),
            })

        _cache_data = zones
        _cache_ts   = now
        log.info("Hive: fetched %d zones", len(zones))
        return zones

    except Exception as exc:
        log.warning("Hive get_climate_data failed: %s", exc)
        return _cache_data


def get_zone_by_id(zone_id: str) -> dict | None:
    """Look up a single zone from cached data."""
    return next(
        (z for z in get_climate_data() if z["id"] == zone_id),
        None,
    )
