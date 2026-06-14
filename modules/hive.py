"""Hive smart heating — TRV temperatures, zone states, boiler status.

Authenticates via Cognito SRP (pyhiveapi), then queries the Beekeeper REST API
directly. We bypass pyhiveapi's startSession() because it crashes when the API
returns an empty homes list (IndexError not caught by the library).

After the June 2026 Hive API change, temperature moved from products.props.temperature
to the devices section (devices[].props.temperature), keyed by device ID.
"""
from __future__ import annotations

import logging
import time
import config

log = logging.getLogger(__name__)

_CACHE_TTL = 60   # seconds

_cache_ts:   float      = 0.0
_cache_data: list[dict] = []


def _fetch_all() -> tuple[list, list, dict]:
    """Authenticate with Hive and return (products, devices, trv_node_sample).

    trv_node_sample is the raw response from /nodes/trv/{first_trv_id} — used to
    probe whether individual TRV endpoints expose temperature after the June 2026
    bulk-API change removed it from /nodes/all.
    """
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
    products = list(parsed.get("products", []))
    devices  = list(parsed.get("devices", []))

    # Probe individual TRV endpoint to see if it has temperature data
    trv_node_sample: dict = {}
    trv_devs = [d for d in devices if d.get("type") == "trv"]
    if trv_devs:
        trv_id = trv_devs[0].get("id", "")
        if trv_id:
            try:
                node_resp = h.api.request("GET",
                    f"{h.api.urls['base']}/nodes/trv/{trv_id}")
                trv_node_sample = node_resp.json() if hasattr(node_resp, 'json') else {}
                log.info("  /nodes/trv/%s keys: %s", trv_id[:20],
                         list(trv_node_sample.keys()) if isinstance(trv_node_sample, dict) else type(trv_node_sample))
                if isinstance(trv_node_sample, dict):
                    nd_props = trv_node_sample.get("props", {})
                    nd_state = trv_node_sample.get("state", {})
                    log.info("  /nodes/trv props keys: %s  state keys: %s",
                             list(nd_props.keys()), list(nd_state.keys()))
                    log.info("  /nodes/trv props.temp=%s state.temp=%s",
                             nd_props.get("temperature"), nd_state.get("temperature"))
            except Exception as e:
                log.info("  /nodes/trv fetch failed: %s", e)

    return products, devices, trv_node_sample


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return round(f, 1) if f != 0.0 else None  # treat 0.0 as no reading
    except (TypeError, ValueError):
        return None


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
    global _cache_ts, _cache_data

    if not config.HIVE_EMAIL or not config.HIVE_PASSWORD:
        return []

    now = time.time()
    if now - _cache_ts < _CACHE_TTL:
        return _cache_data

    try:
        products, devices, _trv_sample = _fetch_all()
        zones: list[dict] = []

        # Build device-id → data map for temperature lookup
        dev_map: dict[str, dict] = {}
        for d in devices:
            did = d.get("id") or d.get("deviceId") or d.get("device_id")
            if did:
                dev_map[did] = d

        log.info("Hive API: %d products, %d devices, %d in dev_map", len(products), len(devices), len(dev_map))

        for p in products:
            ptype = p.get("type", "")
            if ptype not in ("heating", "trvcontrol"):
                continue

            props = p.get("props", {})
            state = p.get("state", {})

            # ── Temperature ──────────────────────────────────────────────────
            # Try in order: props.temperature → nested TRVs → devices section
            current = _safe_float(props.get("temperature"))

            if current is None:
                # Try props.trvs[].props.temperature (nested in product)
                trvs_in_props = props.get("trvs", [])
                if not zones:  # log trvs + consumers structure once
                    log.info("Hive trvs count: %d, consumers count: %d",
                             len(trvs_in_props), len(props.get("consumers", [])))
                    if trvs_in_props:
                        first = trvs_in_props[0]
                        log.info("Hive TRV-in-props keys: %s  props: %s",
                                 list(first.keys()) if isinstance(first, dict) else type(first),
                                 list(first.get("props", {}).keys()) if isinstance(first, dict) else "N/A")
                    consumers = props.get("consumers", [])
                    if consumers:
                        first_c = consumers[0]
                        log.info("Hive consumer keys: %s  props: %s",
                                 list(first_c.keys()) if isinstance(first_c, dict) else type(first_c),
                                 list(first_c.get("props", {}).keys()) if isinstance(first_c, dict) else "N/A")
                if trvs_in_props:
                    temps = []
                    for t in trvs_in_props:
                        if isinstance(t, dict):
                            v = (_safe_float(t.get("props", {}).get("temperature")) or
                                 _safe_float(t.get("state", {}).get("temperature")))
                        else:
                            # t might be a device ID string — look up in dev_map
                            dev_t = dev_map.get(str(t), {})
                            v = (_safe_float(dev_t.get("props", {}).get("temperature")) or
                                 _safe_float(dev_t.get("state", {}).get("temperature")))
                        if v is not None:
                            temps.append(v)
                    if temps:
                        current = round(sum(temps) / len(temps), 1)

            if current is None:
                # Try props.consumers[].props/state.temperature
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
                # Try device map via product id (some APIs link product ↔ device by same ID)
                dev = dev_map.get(p.get("id", ""), {})
                v = (_safe_float(dev.get("props", {}).get("temperature")) or
                     _safe_float(dev.get("state", {}).get("temperature")))
                if v is not None:
                    current = v

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
        temps_found = sum(1 for z in zones if z["current_temp"] is not None)
        log.info("Hive: fetched %d zones, %d with temperature", len(zones), temps_found)
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
