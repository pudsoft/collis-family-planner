"""Home Assistant REST API integration.

Replaces direct Tapo cloud control with local HA calls so device toggling
works even when plugs report 'offline' on the Tapo cloud.

Requirements on the Pi side
----------------------------
1. Home Assistant running (port 8123)
2. TP-Link / Tapo integration added in HA (Settings → Integrations → TP-Link)
3. Long-Lived Access Token created (HA → Profile → Security)
4. HA_URL and HA_TOKEN set in Doppler

Entity IDs
-----------
Each smart_device row can have a ha_entity_id (e.g. switch.kitchen_plug).
Find them in HA → Developer Tools → States.
If ha_entity_id is NULL the toggle falls back to the Tapo cloud module.
"""
from __future__ import annotations

import logging
import requests
import config

log = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.HA_TOKEN}",
        "Content-Type":  "application/json",
    }


def is_configured() -> bool:
    return bool(config.HA_URL and config.HA_TOKEN)


def get_entity_state(entity_id: str) -> bool | None:
    """Return True=on / False=off / None=unknown."""
    if not is_configured():
        return None
    try:
        r = requests.get(
            f"{config.HA_URL}/api/states/{entity_id}",
            headers=_headers(),
            timeout=5,
        )
        if r.status_code == 200:
            s = r.json().get("state", "")
            if s == "on":  return True
            if s == "off": return False
    except Exception as exc:
        log.warning("HA get_entity_state(%s) failed: %s", entity_id, exc)
    return None


def set_entity_state(entity_id: str, on: bool) -> tuple[bool, str | None]:
    """Turn entity on/off. Returns (success, error_msg)."""
    if not is_configured():
        return False, "HA not configured (HA_URL/HA_TOKEN not set)"
    service = "turn_on" if on else "turn_off"
    domain  = entity_id.split(".")[0] if "." in entity_id else "switch"
    try:
        r = requests.post(
            f"{config.HA_URL}/api/services/{domain}/{service}",
            json={"entity_id": entity_id},
            headers=_headers(),
            timeout=8,
        )
        if r.status_code in (200, 201):
            log.info("HA %s %s → OK", service, entity_id)
            return True, None
        msg = f"HA HTTP {r.status_code}"
        log.warning("HA set_entity_state(%s, %s): %s", entity_id, on, msg)
        return False, msg
    except Exception as exc:
        log.warning("HA set_entity_state(%s, %s) exception: %s", entity_id, on, exc)
        return False, str(exc)


def get_all_entity_states(entity_ids: list[str]) -> dict[str, bool | None]:
    """Batch-fetch states for a list of entity IDs. Returns {entity_id: on}."""
    if not is_configured() or not entity_ids:
        return {}
    results: dict[str, bool | None] = {}
    try:
        r = requests.get(
            f"{config.HA_URL}/api/states",
            headers=_headers(),
            timeout=8,
        )
        if r.status_code == 200:
            wanted = set(entity_ids)
            for ent in r.json():
                eid = ent.get("entity_id", "")
                if eid in wanted:
                    s = ent.get("state", "")
                    results[eid] = True if s == "on" else (False if s == "off" else None)
    except Exception as exc:
        log.warning("HA get_all_entity_states failed: %s", exc)
    return results
