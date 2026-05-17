from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# ── Flask ─────────────────────────────────────────────────────────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "change-me-in-dotenv")
PORT         = int(os.getenv("PORT", 8002))
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8002")  # used in NTFY deep-links

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "/home/pi/data/family.db")

# ── Google Calendar ───────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
CALENDAR_ID          = os.getenv(
    "CALENDAR_ID",
    "e24f136a37bcd594d2d7be6b66ebfa994adb20a471f80bd6c5c918ab97ebe6f8@group.calendar.google.com"
)
CALENDAR_REFRESH_SECS = 900  # 15 minutes

# Map Google Calendar colour names → family members
COLOUR_PERSON = {
    "grape":    ["katie"],
    "tomato":   ["paul"],
    "basil":    ["joshua"],
    "flamingo": ["violet"],
    "banana":   ["katie", "paul"],   # Cookie the dog
    "peacock":  ["katie", "paul", "joshua", "violet"],
    # default colour (no colorId set) → Katie
    "default":  ["katie"],
}

# Google colour ID integers → colour name (as returned by the API)
GOOGLE_COLOUR_ID_MAP = {
    "1":  "lavender",
    "2":  "sage",
    "3":  "grape",
    "4":  "flamingo",
    "5":  "banana",
    "6":  "tangerine",
    "7":  "peacock",
    "8":  "graphite",
    "9":  "blueberry",
    "10": "basil",
    "11": "tomato",
}

# ── NTFY ──────────────────────────────────────────────────────────────────────
NTFY_BASE_URL = "https://ntfy.sh"
# Channels are stored per-person in the DB (person_prefs.ntfy_channel).
# These are fallback env-var overrides if set.
NTFY_CHANNEL_PAUL   = os.getenv("NTFY_CHANNEL_PAUL", "")
NTFY_CHANNEL_KATIE  = os.getenv("NTFY_CHANNEL_KATIE", "")
NTFY_CHANNEL_FAMILY = os.getenv("NTFY_CHANNEL_FAMILY", "")

# ── UniFi ─────────────────────────────────────────────────────────────────────
UNIFI_HOST    = os.getenv("UNIFI_HOST", "")
UNIFI_API_KEY = os.getenv("UNIFI_API_KEY", "")
UNIFI_SITE    = os.getenv("UNIFI_SITE", "default")

# Known devices: display name → MAC address (fill in via admin UI or .env)
KNOWN_DEVICES: dict[str, str] = {}
_raw = os.getenv("KNOWN_DEVICES", "")  # format: "name:mac,name:mac"
if _raw:
    for pair in _raw.split(","):
        if ":" in pair:
            parts = pair.split(":", 1)
            KNOWN_DEVICES[parts[0].strip()] = parts[1].strip()

# WiFi networks that admins can toggle
MANAGED_WLANS = [n.strip() for n in os.getenv("MANAGED_WLANS", "TCN_Junior").split(",") if n.strip()]

# ── Weather ───────────────────────────────────────────────────────────────────
# Brundall, Norfolk, UK
WEATHER_LAT = 52.617
WEATHER_LON = 1.469

# ── Alexa Shopping List ───────────────────────────────────────────────────────
# Create a Security Profile at developer.amazon.com → Security Profiles,
# then add the OAuth2 redirect URI: {APP_BASE_URL}/alexa/oauth2callback
ALEXA_CLIENT_ID     = os.getenv("ALEXA_CLIENT_ID", "")
ALEXA_CLIENT_SECRET = os.getenv("ALEXA_CLIENT_SECRET", "")

# ── Admin PIN ─────────────────────────────────────────────────────────────────
ADMIN_PIN = os.getenv("ADMIN_PIN", "1234")  # Katie & Paul set this in .env

# ── People ────────────────────────────────────────────────────────────────────
PEOPLE = ["katie", "paul", "joshua", "violet"]
ADMINS = ["katie", "paul"]

PERSON_DISPLAY = {
    "katie":  {"label": "Katie",  "colour": "#E8589F", "emoji": "💗"},
    "paul":   {"label": "Paul",   "colour": "#E05252", "emoji": "🔴"},
    "joshua": {"label": "Joshua", "colour": "#4A8C5C", "emoji": "🌿"},
    "violet": {"label": "Violet", "colour": "#C97CC9", "emoji": "🌸"},
    "family": {"label": "Family", "colour": "#7B5BA6", "emoji": "🏠"},
}

# ── Before-you-leave rules ────────────────────────────────────────────────────
# Maps keywords in event titles → checklist items to suggest
BEFORE_YOU_LEAVE_RULES = [
    (["swim", "swimming", "pool"],        "Swimming kit & towel"),
    (["school", "class", "lesson"],       "Packed lunch"),
    (["football", "rugby", "sport", "match", "training"], "Sports kit & boots"),
    (["park", "walk", "hike", "outdoor"], "Rainwear"),
    (["holiday", "trip", "travel"],       "Suncream & travel documents"),
    (["vet", "dog"],                      "Cookie's lead & poo bags"),
    (["hospital", "doctor", "dentist", "appointment"], "Referral letter / appointment card"),
]
