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
     │  ├── MySQL HeatWave  (OCI production)
     │  │     or SQLite     (local dev — DB_DRIVER=sqlite)
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
| DB (production) | MySQL HeatWave on OCI (`DB_DRIVER=mysql`) |
| DB (local dev) | SQLite at path set by `DB_PATH` |
| SSH helper | `~/projects/claude-helpers/claude-helpers/cfp_ssh.sh` |

### Useful commands

```bash
# Deploy (git pull + restart service)
~/projects/claude-helpers/claude-helpers/cfp_ssh.sh deploy --force

# Run a remote command
~/projects/claude-helpers/claude-helpers/cfp_ssh.sh run "sudo journalctl -u collis-family-planner -n 50 --no-pager" --force

# Check service status
~/projects/claude-helpers/claude-helpers/cfp_ssh.sh status --force

# Directly on server
sudo systemctl status collis-family-planner
sudo systemctl restart collis-family-planner
```

The `--force` flag auto-switches Tailscale to `tcnskynet@gmail.com` and restores the previous account afterward. Always use it.

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

Production uses **MySQL HeatWave** on OCI (`DB_DRIVER=mysql`). Local dev falls back to **SQLite** (`DB_DRIVER=sqlite`, path from `DB_PATH`).

The `database.py` module provides a sqlite3-compatible `MySQLCompat` wrapper (translates `?` → `%s`, handles named params, returns `Row` dict objects with int-index support).

Schema is created/migrated automatically on startup via `init_db()` in `app.py` — safe to run repeatedly.

### Key tables

| Table | Purpose |
|-------|---------|
| `person_prefs` | Per-person settings (theme, completed_style, ntfy_channel, notif_method, login_pin, presence_mac, visible_pages) |
| `app_settings` | Global key/value store (Google token, VAPID keys, school terms cache, email_enabled) |
| `calendar_events` | Cached Google Calendar events |
| `tasks` | One-off and chore task instances |
| `chore_templates` | Recurring chore definitions |
| `meal_plan` | Weekly meal planner |
| `shopping_items` | Shopping list (includes asda_product_id, is_manual, added_by, added_at) |
| `medicines` | Medicine definitions (see Medicine section) |
| `medicine_doses` | Dose log entries |
| `push_subscriptions` | Web Push subscriptions per person/device |
| `prn_log` | PRN (as-needed) dose log |
| `scheduled_reminders` | Scheduled broadcast messages |
| `known_devices` | UniFi device registry |
| `smart_rooms` | Room layout for the Temperatures/Smart Home page (grid position, floor, zone colour) |
| `smart_devices` | Smart plug/device registry — links to `smart_rooms`, stores provider (tapo/ha), device_id, ha_entity_id |
| `email_accounts` | Per-person IMAP email accounts (label, email_address, app_password — encrypted at rest via Doppler) |
| `event_tasks` | Checklist tasks attached to a specific calendar event |
| `birthdays` | Birthday tracker — date (MM-DD), remind_days, remind_persons, notes |

---

## Modules

| Module | Purpose |
|--------|---------|
| `modules/auth.py` | bcrypt PIN hashing, Google OAuth flow, `GOOGLE_LOGIN_PERSONS` |
| `modules/medicines.py` | Medicine CRUD, dose tracking, multi-dose + frequency support |
| `modules/ntfy.py` | ntfy.sh push notifications |
| `modules/push_notif.py` | Web Push (VAPID) notifications |
| `modules/tasks.py` | Task CRUD, chore scheduling |
| `modules/meals.py` | Meal plan, shopping list |
| `modules/calendar_sync.py` | Google Calendar sync, background thread |
| `modules/weather.py` | Open-Meteo weather fetch |
| `modules/school_terms.py` | Norfolk school term dates scraping |
| `modules/unifi.py` | UniFi network status / device blocking |
| `modules/alexa.py` | Alexa shopping list sync |
| `modules/email_accounts.py` | Email account CRUD (stores IMAP credentials per person) |
| `modules/imap_mail.py` | Gmail IMAP — fetches headers only (never body content) |
| `modules/tapo.py` | TP-Link Tapo cloud API — device discovery, state polling, on/off control via V1 cloud endpoint |
| `modules/home_assistant.py` | Home Assistant local REST API — device toggle (preferred over Tapo cloud when `ha_entity_id` is set on a smart_device) |
| `modules/hive.py` | Hive smart heating — TRV zone temperatures and boiler status via Beekeeper REST API (Cognito SRP auth) |
| `database.py` | MySQL HeatWave connection helper with sqlite3-compatible wrapper |
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
| `dose_times` | TEXT (JSON) | For daily: array of HH:MM strings. For monthly/3monthly: `{"dom": N}` (day of month) |
| `scheduled_time` | TEXT | Legacy single dose time (still used for 1× daily) |
| `stock_count` | REAL | Current stock in doses |
| `reorder_threshold_days` | INTEGER | Warn when this many days of stock remain |
| `active` | INTEGER | 1 = active (reminders fire), 0 = paused |
| `frequency_type` | TEXT | `daily` (default), `monthly`, or `3monthly` |
| `start_date` | TEXT | Optional course start date (YYYY-MM-DD) |
| `end_date` | TEXT | Optional course end date (YYYY-MM-DD) |
| `also_notify` | TEXT | JSON array of extra persons to notify when a dose is taken |
| `notes` | TEXT | e.g. "Take with food" |

### Multi-dose behaviour

- For a 2× daily medicine, the medicines page shows two "Take" buttons (Morning / Evening).
- Each button is tracked independently via `medicine_doses.dose_number`.
- Stock decrements by `daily_dose / doses_per_day` per button press.
- Late warning (⚠️) appears 30 minutes after the scheduled time if a dose hasn't been taken.

### Frequency types

- **daily** — standard reminder every day at the scheduled time(s)
- **monthly** — one dose per month on a specific day (`dose_times.dom`)
- **3monthly** — one dose every 3 months on a specific day

### Also-notify

When `also_notify` is set (e.g. `["katie"]`), the listed persons receive a notification whenever the medicine owner takes a dose. Configured per-medicine in the Admin panel.

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

**Always commit and push before deploying** — the server does `git pull` and can only pull pushed commits.

```bash
# 1. Commit and push
git add <files>
git commit -m "message"
git push origin master

# 2. Deploy to server
~/projects/claude-helpers/claude-helpers/cfp_ssh.sh deploy --force
```

### After Doppler secret changes

```bash
~/projects/claude-helpers/claude-helpers/cfp_ssh.sh run "sudo systemctl restart collis-family-planner" --force
```

### Manual steps on server (if needed)

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
| `PORT` | `8002` |
| `DB_DRIVER` | `mysql` (production) or `sqlite` (local dev) |
| `DB_PATH` | SQLite file path (local dev only) |
| `MYSQL_HOST` | MySQL HeatWave hostname (production) |
| `MYSQL_PORT` | MySQL port (default 3306) |
| `MYSQL_USER` | MySQL username |
| `MYSQL_PASS` | MySQL password |
| `MYSQL_DB` | MySQL database name (`cfp`) |
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID (ends in `.apps.googleusercontent.com`) |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `GOOGLE_EMAIL_PAUL` | Paul's Gmail address |
| `GOOGLE_EMAIL_KATIE` | Katie's Gmail address |
| `NTFY_CHANNEL_PAUL` | Paul's ntfy.sh channel (fallback default) |
| `NTFY_CHANNEL_KATIE` | Katie's ntfy.sh channel (fallback default) |
| `NTFY_CHANNEL_FAMILY` | Family ntfy.sh channel |
| `VAPID_PRIVATE_KEY` | (Optional) Override auto-generated VAPID private key (PEM) |
| `VAPID_PUBLIC_KEY` | (Optional) Override auto-generated VAPID public key (base64url) |
| `VAPID_SUBJECT` | Contact email for VAPID claims |
| `TAPO_EMAIL` | TP-Link account email for Tapo cloud API |
| `TAPO_PASSWORD` | TP-Link account password |
| `HIVE_EMAIL` | Hive account email |
| `HIVE_PASSWORD` | Hive account password |
| `HA_URL` | Home Assistant base URL (e.g. `http://homeassistant.local:8123`) |
| `HA_TOKEN` | Home Assistant long-lived access token |
| `UNIFI_HOST` | UniFi controller hostname |
| `UNIFI_API_KEY` | UniFi API key |
| `UNIFI_SITE` | UniFi site name (default: `default`) |
| `ALEXA_CLIENT_ID` | Amazon Security Profile client ID |
| `ALEXA_CLIENT_SECRET` | Amazon Security Profile client secret |

---

## Feature Reference

### Navigation

Navigation uses a **side drawer** (hamburger menu in the top bar). All pages are accessible from the drawer; which tiles appear on the home grid is per-person configurable via `person_prefs.visible_pages`.

| Page | Route | Description |
|------|-------|-------------|
| Home | `/` | Tile grid — per-person configurable launcher |
| Today | `/dashboard` | Overview widgets (weather, tasks, calendar, meals) |
| Calendar | `/calendar` | Google Calendar view with colour-coded family events |
| To-do | `/tasks` | Tasks and recurring chores |
| Shopping | `/shopping` | Shopping list with ASDA barcode scanner |
| Meal Plan | `/meals` | Meal planner |
| Medicines | `/medicines` | Medicine tracker with day navigation |
| Email | `/email` | IMAP email manager (admin-enabled, per-person) |
| Temperatures | `/smarthome` | Hive TRV room temperatures + TP-Link smart plug controls |
| Energy | `/energy` | Energy monitoring |
| WiFi | `/network` | UniFi device manager (admin only) |
| Settings | `/settings` | Per-person preferences and notifications |
| Admin | `/admin` | Admin panel (admin only) |

### Home grid tiles

Defined in `config.HOME_TILES`. Each tile has an id, label, emoji, URL, and optional `admin_only` flag. Users can show/hide tiles via Settings; the set of visible tiles is stored in `person_prefs.visible_pages`.

| Tile | URL | Admin only |
|------|-----|-----------|
| Today | `/dashboard` | No |
| Calendar | `/calendar` | No |
| To-do | `/tasks` | No |
| ASDA Scanner | `/shopping?scanner=1` | No |
| Shopping | `/shopping` | No |
| Meal Plan | `/meals` | No |
| Medicines | `/medicines` | No |
| Email | `/email` | No |
| WiFi | `/network` | Yes |
| Temperatures | `/smarthome` | No |
| Energy | `/energy` | No |
| Settings | `/settings` | No |
| Admin | `/admin` | Yes |

### Admin panel (`/admin`)

- **Chores** — add/edit/delete recurring chore templates
- **Medicines** — add/edit/delete medicines (name, person, dose, schedule, frequency, active, also_notify)
- **Birthdays** — add/edit/delete birthday entries with reminder days and recipients
- **Login PINs** — set/clear PINs for Joshua, Violet, Family
- **Email Manager** — enable/disable the Email feature site-wide
- **Known Devices** — view/edit the UniFi device registry

### Settings (`/settings`)

- Display preferences (theme, completed task style, forecast days)
- Home grid tile visibility (which tiles appear on `/`)
- Notifications (ntfy channel / browser push / off)
- Change PIN (PIN users only)
- Google Calendar connection (admin only)

### Person selector

The dropdown in the top bar switches whose view you're in. The URL parameter `?person=<name>` controls filtering throughout the app. The `family` view shows everything.

### Smart Home / Temperatures (`/smarthome`)

Combines:
- **Hive** — TRV room temperatures and boiler status (read-only; Jun 2026 API returns empty data for temperature — check module comments)
- **TP-Link Tapo / Home Assistant** — smart plug on/off toggle. If a `smart_device` row has `ha_entity_id` set, HA local REST API is used; otherwise falls back to Tapo cloud

Rooms and devices are managed via the `smart_rooms` and `smart_devices` tables. Rooms have a grid position (grid_row, grid_col, span) and an optional floor (`ground`/`first`).

### Email Manager (`/email`)

- Per-person IMAP accounts stored in `email_accounts`
- Fetches email headers only — from, subject, date (never body content)
- Gmail App Passwords required (standard Gmail password blocked by Google)
- Feature can be enabled/disabled globally from Admin panel (`app_settings.email_enabled`)
