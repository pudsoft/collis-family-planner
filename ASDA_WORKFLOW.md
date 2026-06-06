# ASDA Shopping Automation — Workflow Guide

## Overview

Three scripts work together to maintain a searchable product catalogue and (eventually) automate basket filling.

| Script | Purpose | Run frequency |
|--------|---------|---------------|
| `extract_edge_cookies.js` | Extracts your ASDA session from Edge's cookie store | Before each enrich run |
| `asda_enrich_regulars.js` | Scrapes order history, merges new products into regulars list | Monthly or after a big shop |
| `asda_discover.js` | Full API discovery tool (dev use) | Only when investigating new endpoints |

---

## Regular workflow: keeping the product list fresh

Run these steps monthly, or any time you've done a big shop with items not in the search.

### Step 1 — Extract your Edge session

Edge must be **closed** first.

```powershell
Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
node extract_edge_cookies.js
```

This reads cookies directly from Edge's on-disk database (no browser launch needed) and saves them to `data/asda_session.json`. Takes ~2 seconds.

### Step 2 — Enrich the regulars list

```powershell
Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
node asda_enrich_regulars.js
```

Edge opens automatically on your **Past Orders** page. Click each order to open it (this triggers the API call that loads the item list), then come back to the terminal and press **Enter**.

The script will:
- Extract every product from every order you opened
- Skip products already in the list
- Update `usual_qty` if you've been ordering more than before
- Sort by order frequency (most-ordered items appear first in search)

### Step 3 — Commit and deploy

```powershell
git add data/asda_regulars.json
git commit -m "Update ASDA regulars from order history"
git push origin master
```

Then deploy:
```powershell
& "C:\Claude_Helpers\claude-helpers\cfp_ssh.ps1" deploy
```

The Family Planner shopping page will immediately show the expanded product list.

---

## One-command wrapper

For convenience, `asda_refresh.ps1` does Steps 1–3 in one go:

```powershell
.\asda_refresh.ps1
```

---

## File reference

| File | Description |
|------|-------------|
| `data/asda_regulars.json` | Master product list — 347 items (as of June 2026). Each entry: `product_id`, `name`, `usual_qty`. |
| `data/asda_session.json` | Saved Edge cookies. Valid for ~24–48h. Created by `extract_edge_cookies.js`. **Do not commit** (in .gitignore). |
| `asda_api_calls.json` | Raw API capture from `asda_discover.js`. Not committed. |

---

## How the auth works

| Endpoint | Auth method |
|----------|-------------|
| `www.asda.com` (SFCC) | Bearer JWT — short-lived, issued by the browser session |
| `api2.asda.com` | OCP subscription key (`bc042eff…`) + session cookies |
| Algolia search | Static API key — no session needed |

The session cookies from Edge are what unlock `api2.asda.com`. The enrich script uses a real Edge browser session so Cloudflare doesn't block it.

---

## Troubleshooting

**"Opening in existing browser session"** — Edge is still running. Run `Stop-Process -Name msedge -Force` and try again.

**Extract script fails** — Make sure you're in `C:\Collis Family Planner` and Edge is closed.

**0 orders found** — You didn't click any orders in the browser, or the page didn't finish loading before you pressed Enter.

**New items not showing in the app** — Remember to commit `data/asda_regulars.json` and deploy after enriching.
