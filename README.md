# Edgewater Riverfront Campsite Monitor

Checks **edgewateratlava.com** every **15 minutes** for open **Riverfront** RV sites that fit
a **42 ft Fifth Wheel**, and pushes a phone notification (via [ntfy](https://ntfy.sh))
the moment one becomes bookable. Also sends a **daily heartbeat** (~8 PM Mountain) so you know
it's still running even when there's nothing to report.

> **Make the repo public.** GitHub gives public repos unlimited Actions minutes; a private repo
> only gets 2,000/month, which the 15-minute schedule would exceed. Nothing sensitive is in the
> code — your ntfy topic is stored as a secret, not committed.

- Browser-free: one HTTPS request per stay against the Newbook booking engine.
- Detects real **"Book now"** buttons per category (the authoritative signal), filtered to
  titles containing **"Riverfront"**. The site's "Only N sites available!" banner is ignored.
- Availability depends on rig length — the length filter is always applied, so results match
  what can actually fit your 42 ft fifth wheel.
- Notifies **only on change** (a site going full → available), so no hourly spam.

## What it monitors

Edit [`config.json`](config.json):

```json
{
  "length_ft": 42,
  "equipment_type_id": 3,          // 3 = Fifth Wheel (4=Travel Trailer, 5=Class A, 7=Class C, 11=Tent ...)
  "notify_mode": "on_change",       // or "always" to ping every hour there's availability
  "stays": [
    { "arrival": "2026-08-14", "departure": "2026-08-17" }
  ]
}
```

Add more objects to `stays` to watch multiple date ranges at once. You can override
`length_ft` / `equipment_type_id` per stay.

## One-time setup

### 1. Install ntfy on your phone
App Store / Play Store → **ntfy** → open it → **Subscribe to topic** →
enter your topic (kept private as a GitHub secret below).

### 2. Push this folder to a new GitHub repo
```bash
cd "edgewater-camp-monitor"
git init && git add . && git commit -m "Edgewater riverfront monitor"
# create a repo on github.com (private recommended), then:
git remote add origin git@github.com:<you>/edgewater-camp-monitor.git
git push -u origin main
```

### 3. Add your ntfy topic as a repo secret
GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**
- Name: `NTFY_TOPIC`
- Value: your topic (the same one you subscribed to in the app)

That's it. The workflow runs hourly automatically. You can also trigger it manually:
repo → **Actions → Edgewater Riverfront Monitor → Run workflow**.

## Run it locally (optional)
```bash
NTFY_TOPIC=your-topic python3 monitor.py
```

## Files
- `monitor.py` — the checker + notifier (`--heartbeat` sends the daily status ping)
- `config.json` — dates, rig length, notify mode
- `state.json` — auto-managed; remembers last-seen availability (for notify-on-change)
- `.github/workflows/monitor.yml` — every-15-minutes availability check
- `.github/workflows/heartbeat.yml` — daily "still running" push (02:00 UTC ≈ 8 PM Mountain)
