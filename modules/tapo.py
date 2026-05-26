"""TP-Link Tapo cloud API — device discovery, state polling and control.

Uses the V1 cloud endpoint (wap.tplinkcloud.com) which requires no request
signing. Device on/off state is retrieved via cloud passthrough — no local
network access needed, IoT VLAN isolation is fully maintained.

Known V1 API quirk: getDeviceList always returns status=0 for every device
regardless of actual connectivity. Online/offline is therefore determined by
whether the passthrough call succeeds, not by the status field.
Aliases are returned Base64-encoded and decoded automatically.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import config

import requests

log = logging.getLogger(__name__)

_CLOUD_URL  = "https://wap.tplinkcloud.com"
_TERM_UUID  = "cfp-tapo-v1-integration"
_CACHE_TTL  = 30   # seconds between full device state refreshes

# ── In-memory cache ───────────────────────────────────────────────────────────
_token: str | None       = None
_cache_ts: float         = 0.0
_cache_devices: list     = []


# ── Alias decoding ────────────────────────────────────────────────────────────

def _decode_alias(alias: str) -> str:
    """Tapo cloud V1 returns device names Base64-encoded. Decode if possible."""
    if not alias:
        return alias
    try:
        padded  = alias + "=" * (-len(alias) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        if decoded.isprintable() and any(c.isalpha() for c in decoded):
            return decoded
    except Exception:
        pass
    return alias


# ── Auth ──────────────────────────────────────────────────────────────────────

def _post(payload: dict, token: str | None = None) -> dict:
    url = _CLOUD_URL + (f"?token={token}" if token else "")
    r = requests.post(url, json=payload, timeout=10)
    data = r.json()
    if data.get("error_code", 0) != 0:
        raise RuntimeError(
            f"Tapo cloud error {data.get('error_code')}: {data.get('msg', '')}"
        )
    return data.get("result", {})


def _get_token() -> str:
    global _token
    if _token:
        return _token
    result = _post({
        "method": "login",
        "params": {
            "appType":       "Kasa_Android",
            "cloudUserName": config.TAPO_EMAIL,
            "cloudPassword": config.TAPO_PASSWORD,
            "terminalUUID":  _TERM_UUID,
        },
    })
    _token = result["token"]
    log.info("Tapo: authenticated successfully")
    return _token


def _invalidate_token():
    global _token
    _token = None


# ── Device listing ────────────────────────────────────────────────────────────

def list_cloud_devices() -> list[dict]:
    """Return device list from Tapo cloud with decoded aliases."""
    if not config.TAPO_EMAIL or not config.TAPO_PASSWORD:
        return []
    try:
        token   = _get_token()
        result  = _post({"method": "getDeviceList", "params": {}}, token=token)
        devices = result.get("deviceList", [])
        # Decode Base64-encoded aliases in-place
        for d in devices:
            if d.get("alias"):
                d["alias"] = _decode_alias(d["alias"])
        return devices
    except Exception as exc:
        log.warning("Tapo list_cloud_devices failed: %s", exc)
        _invalidate_token()
        return []


# ── Device state (passthrough) ────────────────────────────────────────────────

def _passthrough(device: dict, request_data: dict) -> dict | None:
    """Send a passthrough command to a device via its cloud appServerUrl."""
    try:
        token   = _get_token()
        app_url = device.get("appServerUrl", _CLOUD_URL)
        url     = f"{app_url}?token={token}"
        payload = {
            "method": "passthrough",
            "params": {
                "deviceId":    device["deviceId"],
                "requestData": json.dumps(request_data),
            },
        }
        r    = requests.post(url, json=payload, timeout=8)
        data = r.json()
        if data.get("error_code", 0) != 0:
            return None
        return json.loads(data["result"]["responseData"])
    except Exception as exc:
        log.warning("Tapo passthrough failed for %s: %s",
                    device.get("alias", "?"), exc)
        return None


def _parse_on_state(resp: dict) -> bool | None:
    """Extract on/off boolean from a get_sysinfo passthrough response."""
    sysinfo = resp.get("system", {}).get("get_sysinfo", {})
    relay   = sysinfo.get("relay_state")
    if relay is None:
        relay = sysinfo.get("device_on")
    return bool(relay) if relay is not None else None


def get_device_state(device: dict) -> bool | None:
    """Return True (on) / False (off) / None (unknown) for a device."""
    resp = _passthrough(device, {"system": {"get_sysinfo": {}}})
    return None if resp is None else _parse_on_state(resp)


def set_device_state(device: dict, on: bool) -> tuple[bool, str | None]:
    """Turn a device on (True) or off (False). Returns (success, error_msg)."""
    try:
        token   = _get_token()
        app_url = device.get("appServerUrl", _CLOUD_URL)
        url     = f"{app_url}?token={token}"
        payload = {
            "method": "passthrough",
            "params": {
                "deviceId":    device["deviceId"],
                "requestData": json.dumps(
                    {"system": {"set_relay_state": {"state": 1 if on else 0}}}
                ),
            },
        }
        r    = requests.post(url, json=payload, timeout=8)
        body = r.json()
        ec   = body.get("error_code", -1)
        if ec != 0:
            msg = body.get("msg") or f"Tapo error {ec}"
            log.warning("Tapo set_device_state failed: error_code=%s msg=%s", ec, msg)
            return False, msg
        _bust_cache()
        return True, None
    except Exception as exc:
        log.warning("Tapo set_device_state exception: %s", exc)
        return False, str(exc)


# ── Cached full status (devices + states) ────────────────────────────────────

def _bust_cache():
    global _cache_ts
    _cache_ts = 0.0


def get_all_device_states() -> list[dict]:
    """
    Return all registered Tapo devices, cached for _CACHE_TTL seconds.

    We skip the passthrough sysinfo probe: the V1 cloud passthrough is
    unreliable for many modern Tapo models and causes every device to
    appear offline even when it is working fine.  Devices are therefore
    assumed online; on/off state is unknown (None) until the user
    toggles a device, at which point set_device_state() succeeds and
    the cache is busted so the next poll can pick up the real state.

    Each dict: {deviceId, alias, deviceType, deviceModel, online, on}
    """
    global _cache_ts, _cache_devices
    now = time.time()
    if now - _cache_ts < _CACHE_TTL:
        return _cache_devices

    devices = list_cloud_devices()
    results = []
    for d in devices:
        results.append({
            "deviceId":    d.get("deviceId", ""),
            "alias":       d.get("alias", d.get("deviceName", "Device")),
            "deviceType":  d.get("deviceType", ""),
            "deviceModel": d.get("deviceModel", ""),
            "online":      True,   # assume reachable; toggle will fail + toast if not
            "on":          None,   # state unknown until first successful toggle
        })
        log.debug("Tapo %-26s  listed", d.get("alias"))

    _cache_devices = results
    _cache_ts      = now
    log.info("Tapo: listed %d devices", len(results))
    return results
