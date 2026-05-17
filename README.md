# Collis Family Planner

ADHD-friendly family command centre. Mobile-first Flask app for the Collis family.

## Features

- **Per-person views** — Family, Katie, Paul, Joshua, Violet
- **Google Calendar** — colour-coded events per person, 14-day lookahead
- **Work meetings** — Paul pushes his day's meetings via `POST /work_calendar`
- **Before you leave** — auto-generated checklist from today's events
- **Tasks** — full CRUD, recurring house chores, executive-function transfer, defer
- **Meals & shopping** — weekly meal planner, auto-generated Asda-friendly shopping list
- **Medicines** — daily dose tracking, stock countdown, reorder alerts via NTFY
- **NTFY** — deep-linked push notifications direct to the relevant page & item
- **UniFi** — toggle WiFi networks, block/kick devices (Katie & Paul)
- **Norfolk term dates** — school term indicator on dashboard

## Pi setup

```bash
git clone https://github.com/pudsoft/collis-family-planner.git
cd collis-family-planner
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env   # fill in your values
mkdir -p /home/pi/data
python app.py
```

Install as a service:
```bash
sudo cp collis-family-planner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now collis-family-planner
```

## Google Calendar setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → enable **Google Calendar API**
3. Create **OAuth 2.0 credentials** (Web application)
4. Add redirect URI: `http://your-pi-ip:8002/calendar/oauth2callback`
5. Copy Client ID & Secret into `.env`
6. Open the app as Katie or Paul → Settings → Connect Google Calendar

## Work meetings (Paul's work PC)

Push today's meetings from your work PC:
```bash
curl -X POST http://your-pi:8002/work_calendar \
  -H "Content-Type: application/json" \
  -d '[{"title":"Standup","start":"2026-05-17T09:00:00","end":"2026-05-17T09:15:00","agenda":"Daily sync"}]'
```

## Required `.env` keys

See `.env.example` for full list. Minimum to run:
- `SECRET_KEY`
- `APP_BASE_URL`
- `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`
- `ADMIN_PIN`
