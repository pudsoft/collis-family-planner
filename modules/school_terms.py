"""Norfolk County Council school term dates scraper with monthly disk cache.

On import, the module immediately tries to load from the disk cache so that
is_term_time() never blocks on the first request.  If the disk cache is
missing or stale the live fetch is attempted in the background; failed
fetches are rate-limited (5-minute back-off) so one bad response cannot
make the dashboard slow on every load.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date
from pathlib import Path

import requests

log = logging.getLogger(__name__)

CACHE_FILE     = Path(__file__).parent.parent / "term_dates.json"
CACHE_TTL      = 30 * 24 * 3600   # 30 days
_RETRY_AFTER   = 300               # back-off 5 minutes after a failed fetch
_HTTP_TIMEOUT  = 5                 # seconds — term dates are static, no need to wait long

SOURCE_URL = (
    "https://www.norfolk.gov.uk/children-and-families/schools-and-learning"
    "/school-terms-and-holidays"
)

# In-memory state
_cache: dict = {}
_fetch_attempted_at: float = 0.0   # timestamp of last live-fetch attempt
_cache_lock = threading.Lock()


# ── Disk helpers ──────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            if time.time() - data.get("fetched_at", 0) < CACHE_TTL:
                return data
        except Exception:
            pass
    return {}


def _save_cache(data: dict):
    try:
        data["fetched_at"] = time.time()
        CACHE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("school_terms: could not save cache: %s", exc)


# ── Live fetch ────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> str | None:
    """Parse UK date string like '4 September 2024' → '2024-09-04'."""
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", s, re.IGNORECASE)
    if m:
        day, month_str, year = m.group(1), m.group(2).lower(), m.group(3)
        month = months.get(month_str)
        if month:
            return f"{year}-{month:02d}-{int(day):02d}"
    return None


def fetch_term_dates(force: bool = False) -> dict:
    """Return (and cache) term/holiday data from Norfolk CC.

    Non-forced calls are rate-limited: if a live fetch was already tried
    within _RETRY_AFTER seconds it returns whatever is in _cache immediately.
    """
    global _cache, _fetch_attempted_at

    # 1. Disk cache still valid? (handles server restarts cheaply)
    if not force:
        with _cache_lock:
            if _cache:
                return _cache
        cached = _load_cache()
        if cached:
            with _cache_lock:
                _cache = cached
            return cached

    # 2. Rate-limit live fetches to prevent hammering on failure
    now = time.time()
    with _cache_lock:
        if not force and (now - _fetch_attempted_at) < _RETRY_AFTER:
            return _cache or {"terms": [], "holidays": []}
        _fetch_attempted_at = now

    # 3. Live fetch
    try:
        resp = requests.get(
            SOURCE_URL, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        html = resp.text

        pattern = re.compile(
            r"(\d{1,2}\s+\w+\s+\d{4})\s+to\s+(\d{1,2}\s+\w+\s+\d{4})",
            re.IGNORECASE,
        )
        terms, hols = [], []
        for m in pattern.finditer(html):
            start = _parse_date(m.group(1))
            end   = _parse_date(m.group(2))
            if start and end:
                s = date.fromisoformat(start)
                e = date.fromisoformat(end)
                entry = {"start": start, "end": end}
                (hols if (e - s).days < 21 else terms).append(entry)

        data = {"terms": terms, "holidays": hols}
        _save_cache(data)
        with _cache_lock:
            _cache = data
        log.info("school_terms: fetched %d term periods, %d holiday periods",
                 len(terms), len(hols))
        return data

    except Exception as exc:
        log.warning("school_terms: live fetch failed: %s", exc)
        with _cache_lock:
            if not _cache:
                # Set a non-empty sentinel so callers don't retry immediately
                _cache = {"terms": [], "holidays": []}
            return _cache


# ── Public API ────────────────────────────────────────────────────────────────

def is_term_time(check_date: date = None) -> bool:
    d = check_date or date.today()
    with _cache_lock:
        data = _cache
    if not data:
        data = fetch_term_dates()
    for term in data.get("terms", []):
        if date.fromisoformat(term["start"]) <= d <= date.fromisoformat(term["end"]):
            return True
    return False


def is_school_holiday(check_date: date = None) -> bool:
    d = check_date or date.today()
    with _cache_lock:
        data = _cache
    if not data:
        data = fetch_term_dates()
    for hol in data.get("holidays", []):
        if date.fromisoformat(hol["start"]) <= d <= date.fromisoformat(hol["end"]):
            return True
    return False


# ── Pre-load from disk on import so the first is_term_time() is instant ───────
def _background_refresh():
    """Fetch live data in the background if disk cache is missing/stale."""
    if not _load_cache():
        log.info("school_terms: disk cache missing — fetching in background")
        fetch_term_dates(force=True)


_startup_cache = _load_cache()
if _startup_cache:
    _cache = _startup_cache
else:
    # No disk cache — kick off a background refresh so the first live load
    # doesn't block a user request
    threading.Thread(target=_background_refresh, daemon=True,
                     name="school-terms-init").start()
