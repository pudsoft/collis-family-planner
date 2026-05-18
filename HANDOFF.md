# Collis Family Planner — Handoff Document

## What is this?

A private, mobile-first web app running on the family Raspberry Pi. It acts as a central command centre for the Collis family: Paul, Katie, Joshua, Violet, and Cookie the dog. Built for ADHD brains — minimal friction, colour-coded per person, push reminders.

**URL:** `http://192.168.1.2:8002` (home network only)  
**Pi location:** Raspberry Pi at 192.168.1.2  
**Service name:** `collis-family-planner` (systemd)

---

## Tech stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.7.3 (Raspbian Buster) |
| Framework | Flask 2.2.5 |
| Database | SQLite (`/home/pi/data/family.db`) |
| Templates | Jinja2 |
| CSS/JS | Vanilla — no build step |
| Notifications | NTFY |
| Calendar | Google Calendar API (OAuth2 PKCE) |
| Network | UniFi Network API |

---

## Person → colour mapping

The Google Calendar is colour-coded. The app reads event colours to determine who an event belongs to:

| Colour | Person(s) |
|--------|-----------|
| Grape | Katie |
| Tomato | Paul |
| Basil | Joshua |
| Flamingo | Violet |
| Banana | Katie & Paul (Cookie/family) |
| Peacock | Everyone |

---

## Features

### Dashboard
- Weather strip (Open-Meteo, Brundall coords)
- **Childcare warning** — if Katie has a "Toby" event (she's at work) and Paul has no "A/L" event, and either Joshua or Violet have nothing between 09:00–17:30 → red alert shown
- **Drop-off today** — shown to Paul/Family view; shows Joshua and Violet's first timed event of the day (where and when to take them)
- Today's calendar events, colour-coded
- Today's tasks (filtered to current person)
- Medicines not yet taken today

### Calendar
- Syncs from Google Calendar every 15 minutes (background thread)
- Soft-cancels events that disappear from Google (shown faded with strikethrough)
- Paul's work meetings pushed from his work PC via `POST /work_calendar` with NOW/SOON/LATER/ENDED badges
- "Before you leave" checklist auto-generated from upcoming events

### Tasks
- Full CRUD; assignable to any family member or "anyone"
- Recurring house chores with configurable intervals
- Executive Function Transfer — Katie/Paul can bounce an "anyone" task to each other
- Defer to tomorrow (or specific date)

### Meals & Shopping
- Week view meal planner (Mon–Sun, three meals/day)
- Shopping list with category grouping
- Alexa shopping list sync (requires Amazon credentials in `.env`)

### Medicines
- Daily dose tick-off with timestamp
- Stock countdown and reorder alerts
- Covers Cookie (dog) as well as family members
- **PRN / as-needed** — log ad-hoc paracetamol, ibuprofen (with 4h/6h next-safe-dose indicator), and temperature readings

### Network (admin only)
- Toggle Wi-Fi networks on/off
- Show Wi-Fi password + QR code (admin PIN required)
- Block/unblock known devices
- Live polling every 10 seconds

### Settings
- Per-person: completed task style (fade/collapse), dark mode, NTFY channel
- Admin: house chores, medicine inventory, meal templates, known devices, Google Calendar re-auth, school term dates

---

## Admin access

Admin = Paul or Katie. Controlled by `config.ADMINS`.

A full-screen PIN keypad appears for destructive admin actions (block device, toggle Wi-Fi, view password). PIN is set via `ADMIN_PIN` in `/home/pi/collis-family-planner/.env`.

Default PIN: **1234** — change this.

---

## Deployment

### Restart the service
```bash
sudo systemctl restart collis-family-planner
sudo systemctl status collis-family-planner
```

### View logs
```bash
journalctl -u collis-family-planner -f
```

### Update from GitHub
```bash
cd /home/pi/collis-family-planner
git pull
sudo systemctl restart collis-family-planner
```

### Service file location
`/etc/systemd/system/collis-family-planner.service`

---

## Environment variables

All secrets live in `/home/pi/collis-family-planner/.env`. Never commit this file.

```
SECRET_KEY=...
ADMIN_PIN=1234
DB_PATH=/home/pi/data/family.db

GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
APP_BASE_URL=http://192.168.1.2:8002

NTFY_CHANNEL_PAUL=...
NTFY_CHANNEL_KATIE=...

UNIFI_HOST=https://192.168.1.1
UNIFI_API_KEY=...
UNIFI_SITE=default

ALEXA_CLIENT_ID=...
ALEXA_CLIENT_SECRET=...
```

---

## Google Calendar re-authorisation

If the calendar stops syncing:
1. Open the app as Paul or Katie
2. Go to Settings → Google Calendar → Connect
3. Complete the Google OAuth flow
4. Token is stored in the database — no files to manage

---

## File structure

```
/home/pi/collis-family-planner/
├── app.py                  # All Flask routes
├── config.py               # Constants and env-var loading
├── modules/
│   ├── calendar_sync.py    # Google Calendar + work meetings + childcare logic
│   ├── weather.py          # Open-Meteo weather
│   ├── tasks.py            # Task CRUD + chore scheduler
│   ├── meals.py            # Meal planner + shopping list
│   ├── medicines.py        # Medicine tracking + PRN logging
│   ├── ntfy.py             # Push notifications
│   ├── unifi.py            # UniFi network control
│   ├── school_terms.py     # Norfolk CC term dates
│   └── alexa.py            # Amazon Alexa shopping list
├── static/
│   ├── css/main.css
│   └── js/app.js
└── templates/
    ├── base.html           # Shell, nav, PIN keypad
    ├── dashboard.html
    ├── calendar.html
    ├── tasks.html
    ├── meals.html
    ├── medicines.html
    ├── network.html
    ├── settings.html
    └── admin.html
```

---

## Childcare warning logic

Fires on the dashboard when all three conditions are true:
1. Katie has a calendar event containing "Toby" today (she's at work)
2. Paul has no event containing "A/L" or "Annual Leave" today (he's not on holiday)
3. Joshua or Violet (or both) have no timed calendar events between 09:00 and 17:30

The warning shows which child(ren) have no coverage and links to the calendar.

---

## Known limitations / future work

- Alexa integration requires Amazon Security Profile credentials (ask Paul)
- UniFi device MAC addresses need populating in Admin → Known Devices
- Work calendar push requires the companion script running on Paul's work PC
- No offline mode — requires home network
- Norfolk term dates cached from norfolk.gov.uk, refresh monthly
