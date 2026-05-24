# Collis Family Planner — Developer & Admin Documentation

> Live at **https://collisfamilyplanner.ddns.net**  
> Server: Oracle Cloud (Ubuntu 22.04) · Reverse proxy: Caddy 2 · Secrets: Doppler  
> Repo: https://github.com/pudsoft/collis-family-planner

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Infrastructure](#infrastructure)
3. [Authentication](#authentication)
4. [Database](#database)
5. [Modules](#modules)
6. [Medicine Tracking](#medicine-tracking)
7. [Push Notifications](#push-notifications)
8. [PWA / Offline Support](#pwa--offline-support)
9. [Deployment](#deployment)
10. [Doppler Secrets Reference](#doppler-secrets-reference)
11. [Feature Reference](#feature-reference)

---

## Architecture Overview

```
Browser / PWA
     │
     ▼
Caddy 2 (TLS termination, reverse proxy)
     │
     ▼
Flask app  (app.py, port 8002)
     │  ├── SQLite  (/mnt/app-data/cfp/family.db)
     │  ├── Background threads
     │  │     ├── calendar_sync  (Google Calendar, every 15 min)
     │  │     └── med_reminders  (medicine reminders, every 60 s)
     │  └── Modules (see below)
     │
Doppler (secrets / env vars)
```

---

## Infrastructure

| Component | Detail |
|-----------|--------|
| Server | Oracle Cloud Free Tier, Ubuntu 22.04 |
| IP (Tailscale) | `100.111.136.33` |
| DNS | NOIP DDNS → `collisfamilyplanner.ddns.net` |
| TLS | Caddy 2 (auto Let's Encrypt) |
| Process manager | systemd (`collis-family-planner.service`) |
| Secrets | Doppler (scoped to `/home/ubuntu/collis-family-planner`) |
| DB | SQLite at `/mnt/app-data/cfp/family.db` |
| SSH helper | `C:\CFP_Helpers\claude-helpers\oracle_ssh.py` |

### Useful commands

```bash
# SSH via helper
cd C:\CFP_Helpers\claude-helpers
python oracle_ssh.py run "sudo journalctl -u collis-family-planner -n 50 --no-pager"
python oracle_ssh.py deploy        # git pull + pip install + restart

# Directly on server
sudo systemctl status collis-family-planner
sudo systemctl restart collis-family-planner
sqlite3 /mnt/app-data/cfp/family.db ".tables"
```

---

## Authentication

### People and roles

| Person | Login method | Admin |
|--------|-------------|-------|
| Paul | Google OAuth | ✅ |
| Katie | Google OAuth | ✅ |
| Joshua | PIN | ❌ |
| Violet | PIN | ❌ |
| Family | PIN (shared passcode) | ❌ |

- Google users cannot log in with a PIN (blocked both UI and server-side).
- PINs are bcrypt-hashed and stored in `person_prefs.login_pin`.
- Sessions are permanent (30-day cookie).

### Google OAuth setup

- Credentials live in Doppler: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- Authorised redirect URI: `https://collisfamilyplanner.ddns.net/login/google/callback`
- Email → person mapping: `GOOGLE_EMAIL_PAUL`, `GOOGLE_EMAIL_KATIE` in Doppler
- Only emails listed in Doppler are allowed; anyone else sees "email not authorised"

### Adding a new Google user

1. Add `GOOGLE_EMAIL_<NAME>=their@gmail.com` to Doppler
2. Add `"<name>"` to `config.PEOPLE` and `modules/auth.GOOGLE_LOGIN_PERSONS`
3. Add to `config.PERSON_DISPLAY`
4. Restart service

---

## Database

SQLite file at `/mnt/app-data/cfp/family.db`. Schema is created/migrated automatically on startup via `init_db()` in `app.py` — safe to run repeatedly.

### Key tables

| Table | Purpose |
|-------|---------|
| `person_prefs` | Per-person settings (theme, ntfy channel, notification method, PIN) |
| `app_settings` | Global key/value store (Google token, VAPID keys, school terms cache) |
| `calendar_events` | Cached Google Calendar events |
| `tasks` | One-off and chore task instances |
| `chore_templates` | Recurring chore definitions |
| `meal_plan` | Weekly meal planner |
| `shopping_items` | Shopping list |
| `medicines` | Medicine definitions (see Medicine section) |
| `medicine_doses` | Dose log entries |
| `push_subscriptions` | Web Push subscriptions per person/device |
| `prn_log` | PRN (as-needed) dose log |
| `scheduled_reminders` | (Reserved for future scheduled messages) |
| `known_devices` | UniFi device registry |

---

## Modules

| Module | Purpose |
|--------|---------|
| `modules/auth.py` | bcrypt PIN hashing, Google OAuth flow, `GOOGLE_LOGIN_PERSONS` |
| `modules/medicines.py` | Medicine CRUD, dose tracking, multi-dose support |
| `modules/ntfy.py` | ntfy.sh push notifications |
| `modules/push_notif.py` | Web Push (VAPID) notifications |
| `modules/tasks.py` | Task CRUD, chore scheduling |
| `modules/meals.py` | Meal plan, shopping list |
| `modules/calendar_sync.py` | Google Calendar sync, background thread |
| `modules/weather.py` | Open-Meteo weather fetch |
| `modules/school_terms.py` | Norfolk school term dates scraping |
| `modules/unifi.py` | UniFi network status / device blocking |
| `modules/alexa.py` | Alexa shopping list sync |
| `database.py` | MySQL connection helper (for future migration) |
| `config.py` | All env-var backed configuration |

---

## Medicine Tracking

### Medicine fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | TEXT | e.g. "Elvanse 50mg" |
| `person` | TEXT | Owner (katie / paul / joshua / violet / cookie) |
| `daily_dose` | REAL | Total units taken per day (for stock tracking) |
| `doses_per_day` | INTEGER | How many separate doses (1, 2, 3, 4) |
| `dose_times` | TEXT (JSON) | Array of HH:MM strings, one per dose slot e.g. `["08:00","20:00"]` |
| `scheduled_time` | TEXT | Legacy single dose time (still used for 1× daily) |
| `stock_count` | REAL | Current stock in doses |
| `reorder_threshold_days` | INTEGER | Warn when this many days of stock remain |
| `active` | INTEGER | 1 = active (reminders fire), 0 = paused |
| `notes` | TEXT | e.g. "Take with food" |

### Multi-dose behaviour

- For a 2× daily medicine, the medicines page shows two "Take" buttons (Morning / Evening).
- Each button is tracked independently via `medicine_doses.dose_number`.
- Stock decrements by `daily_dose / doses_per_day` per button press.
- Late warning (⚠️) appears 30 minutes after the scheduled time if a dose hasn't been taken.

### Active toggle

Setting a medicine to **inactive** (Paused):
- Stops all reminders for that medicine
- The card still appears on the medicines page with a "paused" label
- Dose logging still works normally
- Use this for short courses, seasonal medications, or anything temporarily stopped

### Day navigation

The medicines page has ‹ › navigation to view and log doses on past days. Future dates are blocked.

---

## Push Notifications

Each person can choose their notification method in **Settings → Notifications**:

### ntfy.sh

- User enters their ntfy.sh channel name (a private random string, e.g. `collis-paul-meds-abc123`)
- Download the ntfy app on iOS/Android and subscribe to the channel
- Free, no account required
- Reminders arrive as native notifications

### Browser / PWA Push

- Click **"Subscribe this device"** in Settings — browser asks for notification permission
- Subscription is saved to `push_subscriptions` table (one row per device)
- Notifications delivered via the service worker even when the app is backgrounded
- VAPID keys are auto-generated on first use and stored in `app_settings`
- To override with your own keys, set `VAPID_PRIVATE_KEY` and `VAPID_PUBLIC_KEY` in Doppler

### Off

No reminders sent for this person.

### Reminder timing

A background thread (`med_reminders`) runs every 60 seconds and:
1. Queries all active medicines with scheduled times
2. For each dose slot due within the last 60 seconds
3. Checks whether the dose has already been logged
4. If not, sends a reminder via the person's chosen method

---

## PWA / Offline Support

The app is installable as a PWA on iOS and Android.

| File | Purpose |
|------|---------|
| `static/manifest.json` | App name, icons, theme colour, start URL |
| `static/sw.js` | Service worker: cache-first static assets, network-first pages, push handler |
| `static/icons/icon-192.png` | Home screen icon |
| `static/icons/icon-512.png` | Splash screen icon |
| `static/icons/apple-touch-icon.png` | iOS home screen icon |
| `templates/offline.html` | Shown when network unavailable |

### Installing on iOS

1. Open Safari → navigate to the site
2. Share → Add to Home Screen
3. The app opens full-screen with the purple house icon

### Installing on Android

Chrome will prompt automatically, or use the browser menu → "Add to Home Screen".

---

## Deployment

### Standard deploy (code change)

```bash
# From dev machine:
cd C:\CFP_Helpers\claude-helpers
python oracle_ssh.py deploy
# This runs: git pull + pip install -r requirements.txt + systemctl restart
```

### After Doppler secret changes

```bash
python oracle_ssh.py run "sudo systemctl restart collis-family-planner"
```

### Manual steps on server

```bash
cd /home/ubuntu/collis-family-planner
git pull origin master
venv/bin/pip install -r requirements.txt
sudo systemctl restart collis-family-planner
sudo systemctl status collis-family-planner
```

### Caddy config

Located at `/etc/caddy/Caddyfile`. Reverse-proxies HTTPS → `localhost:8002`.  
Caddy log: `/var/log/caddy/cfp-access.log` (must be owned by `caddy:caddy`).

---

## Doppler Secrets Reference

| Secret | Description |
|--------|-------------|
| `SECRET_KEY` | Flask session signing key |
| `APP_BASE_URL` | `https://collisfamilyplanner.ddns.net` |
| `DB_PATH` | `/mnt/app-data/cfp/family.db` |
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID (ends in `.apps.googleusercontent.com`) |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `GOOGLE_EMAIL_PAUL` | Paul's Gmail address |
| `GOOGLE_EMAIL_KATIE` | Katie's Gmail address |
| `NTFY_CHANNEL_PAUL` | Paul's ntfy.sh channel (fallback default) |
| `NTFY_CHANNEL_KATIE` | Katie's ntfy.sh channel (fallback default) |
| `VAPID_PRIVATE_KEY` | (Optional) Override auto-generated VAPID private key (PEM) |
| `VAPID_PUBLIC_KEY` | (Optional) Override auto-generated VAPID public key (base64url) |
| `VAPID_SUBJECT` | Contact email for VAPID claims |
| `PORT` | `8002` |

---

## Feature Reference

### Navigation

| Tab | Route | Description |
|-----|-------|-------------|
| Home | `/dashboard` | Overview widgets (weather, tasks, calendar, meals) |
| Calendar | `/calendar` | Google Calendar view with colour-coded family events |
| Tasks | `/tasks` | Tasks and recurring chores |
| Meals | `/meals` | Meal planner and shopping list |
| Meds | `/medicines` | Medicine tracker with day navigation |
| Network | `/network` | UniFi device manager (admin only) |

### Admin panel (`/admin`)

- **Chores** — add/edit/delete recurring chore templates
- **Medicines** — add/edit/delete medicines (name, person, dose, schedule, active)
- **Devices** — mark devices as protected (hidden from block controls)
- **Login PINs** — set/clear PINs for Joshua, Violet, Family

### Settings (`/settings`)

- Display preferences (theme, completed task style, forecast days)
- Notifications (ntfy / browser push / off)
- Change PIN (PIN users only)
- Google Calendar connection (admin only)

### Person selector

The dropdown in the top bar switches whose view you're in. The URL parameter `?person=<name>` controls filtering throughout the app. The `family` view shows everything.
