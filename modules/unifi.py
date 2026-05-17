"""UniFi Network control — WiFi toggle and device block/kick.
Extends the pattern from the Rythm project unifi_portforward.py.
"""
from __future__ import annotations

import logging
import urllib3
import requests
from config import UNIFI_HOST, UNIFI_API_KEY, UNIFI_SITE

log = logging.getLogger(__name__)


def _session():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    s.headers.update({"X-API-KEY": UNIFI_API_KEY})
    s.verify = False
    return s


def _base():
    return f"https://{UNIFI_HOST}/proxy/network/api/s/{UNIFI_SITE}"


def _configured() -> bool:
    if not UNIFI_HOST or not UNIFI_API_KEY:
        log.warning("UNIFI_HOST or UNIFI_API_KEY not set — skipping UniFi call")
        return False
    return True


# ── WiFi networks ─────────────────────────────────────────────────────────────

def list_wlans() -> list[dict]:
    """Return all WLANs with id, name, enabled state."""
    if not _configured():
        return []
    try:
        s = _session()
        resp = s.get(f"{_base()}/rest/wlanconf", timeout=10)
        resp.raise_for_status()
        return [
            {"id": w["_id"], "name": w.get("name", ""), "enabled": w.get("enabled", False)}
            for w in resp.json().get("data", [])
        ]
    except Exception as e:
        log.warning("list_wlans failed: %s", e)
        return []


def set_wlan_enabled(ssid_name: str, enabled: bool) -> bool:
    if not _configured():
        return False
    try:
        s = _session()
        resp = s.get(f"{_base()}/rest/wlanconf", timeout=10)
        resp.raise_for_status()
        wlan_id = None
        for w in resp.json().get("data", []):
            if w.get("name") == ssid_name:
                wlan_id = w["_id"]
                break
        if not wlan_id:
            log.warning("WLAN '%s' not found", ssid_name)
            return False
        put = s.put(f"{_base()}/rest/wlanconf/{wlan_id}", json={"enabled": enabled}, timeout=10)
        put.raise_for_status()
        log.info("WLAN '%s' %s", ssid_name, "enabled" if enabled else "disabled")
        return True
    except Exception as e:
        log.warning("set_wlan_enabled('%s', %s) failed: %s", ssid_name, enabled, e)
        return False


# ── Device management ─────────────────────────────────────────────────────────

def _stamgr(cmd: str, mac: str) -> bool:
    if not _configured():
        return False
    try:
        s = _session()
        resp = s.post(f"{_base()}/cmd/stamgr", json={"cmd": cmd, "mac": mac}, timeout=10)
        resp.raise_for_status()
        log.info("stamgr %s → %s", cmd, mac)
        return True
    except Exception as e:
        log.warning("stamgr %s %s failed: %s", cmd, mac, e)
        return False


def block_device(mac: str) -> bool:
    return _stamgr("block-sta", mac)


def unblock_device(mac: str) -> bool:
    return _stamgr("unblock-sta", mac)


def kick_device(mac: str) -> bool:
    return _stamgr("kick-sta", mac)


def list_connected_clients() -> list[dict]:
    """Return currently connected clients (name, mac, ip, hostname)."""
    if not _configured():
        return []
    try:
        s = _session()
        resp = s.get(f"{_base()}/stat/sta", timeout=10)
        resp.raise_for_status()
        return [
            {
                "mac":      c.get("mac", ""),
                "hostname": c.get("hostname", c.get("name", "")),
                "ip":       c.get("ip", ""),
                "blocked":  c.get("blocked", False),
            }
            for c in resp.json().get("data", [])
        ]
    except Exception as e:
        log.warning("list_connected_clients failed: %s", e)
        return []
