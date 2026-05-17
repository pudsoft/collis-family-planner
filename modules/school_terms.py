"""Norfolk County Council school term dates scraper with monthly cache."""

import json
import logging
import re
import time
from datetime import date
from pathlib import Path

import requests

log = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent / "term_dates.json"
CACHE_TTL  = 30 * 24 * 3600  # 30 days
SOURCE_URL = (
    "https://www.norfolk.gov.uk/children-and-families/schools-and-learning"
    "/school-terms-and-holidays"
)

_cache: dict = {}


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
    data["fetched_at"] = time.time()
    CACHE_FILE.write_text(json.dumps(data, indent=2))


def _parse_date(s: str) -> str | None:
    """Try to parse a UK date string like '4 September 2024' → '2024-09-04'."""
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
    global _cache
    if not force:
        cached = _load_cache()
        if cached:
            _cache = cached
            return cached

    try:
        resp = requests.get(SOURCE_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text

        # Extract date ranges — Norfolk CC lists them as "D Month YYYY to D Month YYYY"
        pattern = re.compile(
            r"(\d{1,2}\s+\w+\s+\d{4})\s+to\s+(\d{1,2}\s+\w+\s+\d{4})",
            re.IGNORECASE,
        )
        terms  = []
        hols   = []
        for m in pattern.finditer(html):
            start = _parse_date(m.group(1))
            end   = _parse_date(m.group(2))
            if start and end:
                # Rough heuristic: holiday blocks are short, term blocks are long
                s = date.fromisoformat(start)
                e = date.fromisoformat(end)
                days = (e - s).days
                entry = {"start": start, "end": end}
                if days < 21:
                    hols.append(entry)
                else:
                    terms.append(entry)

        data = {"terms": terms, "holidays": hols}
        _save_cache(data)
        _cache = data
        log.info("Fetched %d term periods, %d holiday periods from Norfolk CC", len(terms), len(hols))
        return data
    except Exception as e:
        log.warning("Failed to fetch term dates: %s", e)
        return _cache or {"terms": [], "holidays": []}


def is_term_time(check_date: date = None) -> bool:
    d = check_date or date.today()
    data = _cache or fetch_term_dates()
    for term in data.get("terms", []):
        if date.fromisoformat(term["start"]) <= d <= date.fromisoformat(term["end"]):
            return True
    return False


def is_school_holiday(check_date: date = None) -> bool:
    d = check_date or date.today()
    data = _cache or fetch_term_dates()
    for hol in data.get("holidays", []):
        if date.fromisoformat(hol["start"]) <= d <= date.fromisoformat(hol["end"]):
            return True
    return False
