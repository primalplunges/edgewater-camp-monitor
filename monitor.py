#!/usr/bin/env python3
"""
Edgewater @ Lava Hot Springs — Riverfront campsite availability monitor.

Polls the Newbook booking engine for the configured stay(s) and equipment,
finds every RIVERFRONT category showing a real "Book now" button, and pushes a
notification (via ntfy) when a site is newly available.

Reliable, browser-free: one HTTPS POST per stay. Cloudflare's only bot check
(__cf_bm cookie) is satisfied by a normal browser User-Agent + a warm-up GET.

Config lives in config.json. State (to notify only on change) lives in state.json.
Notifications go to the ntfy topic in the NTFY_TOPIC env var (falls back to
config.json "ntfy_topic").
"""
import os, re, sys, json, html, calendar, datetime, subprocess, pathlib

HERE = pathlib.Path(__file__).parent
BASE = "https://edgewateratlava.com/wp-content/plugins/newbook-online/includes/api.php"
PAGE = "https://edgewateratlava.com/book-online/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
COOKIE_JAR = "/tmp/edgewater_cookies.txt"
STATE_FILE = HERE / "state.json"
BOOK_URL = "https://edgewateratlava.com/book-online/"


def curl(url, data=None, load_cookies=False):
    cmd = ["curl", "-sS", "--max-time", "45", "-A", UA,
           "-H", "X-Requested-With: XMLHttpRequest", "-c", COOKIE_JAR]
    if load_cookies:
        cmd += ["-b", COOKIE_JAR]
    if data:
        for k, v in data.items():
            cmd += ["--data-urlencode", f"{k}={v}"]
    cmd += [url]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout


def pretty(d):
    dt = datetime.date.fromisoformat(d)
    return f"{calendar.month_name[dt.month]} {dt.day} {dt.year}"


def nights(a, b):
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


def fetch(arrival, departure, length, equip_type):
    curl(PAGE)  # warm up cookies (cf_bm + PHPSESSID)
    form = {
        "period_from": arrival, "period_to": departure,
        "available_from": pretty(arrival), "available_to": pretty(departure),
        "nights": str(nights(arrival, departure)),
        "adults": "2", "children": "0", "infants": "0", "animals": "0",
        "equipment_measurement_unit": "ft",
        "equipment_length": str(length),
        "equipment_type": str(equip_type),
    }
    url = BASE + "?newbook_api_action=availability_chart_responsive"
    return curl(url, data=form, load_cookies=True)


def available_riverfront(h):
    """Return {category_id: {'name', 'price'}} for RIVERFRONT categories that
    have a real 'Book now' button for the full stay."""
    out = {}
    for m in re.finditer(r"<button([^>]*aria-label='Book now'[^>]*)>", h):
        attrs = m.group(1)
        cid = (re.search(r"category_id='(\d+)'", attrs) or [None, None])[1]
        cname = html.unescape((re.search(r"category_name='([^']*)'", attrs) or [None, ''])[1])
        amt = (re.search(r"full_amount='([\d.]+)'", attrs) or [None, ''])[1]
        if cid and "riverfront" in cname.lower():
            out.setdefault(cid, {"name": cname, "price": amt})
    return out


def notify(topic, title, message, tags="tent,ocean", priority="high"):
    if not topic:
        print("  [notify] NTFY_TOPIC not set — skipping push")
        return
    url = f"https://ntfy.sh/{topic}"
    cmd = ["curl", "-sS", "--max-time", "20",
           "-H", f"Title: {title}",
           "-H", f"Priority: {priority}",
           "-H", f"Tags: {tags}",
           "-H", f"Click: {BOOK_URL}",
           "-d", message, url]
    subprocess.run(cmd, capture_output=True, text=True)
    print(f"  [notify] pushed to ntfy topic '{topic}'")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main(heartbeat=False):
    cfg = json.loads((HERE / "config.json").read_text())
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    always = cfg.get("notify_mode", "on_change") == "always"
    state = load_state()
    changed_state = False
    heartbeat_lines = []       # one summary line per stay, for the daily heartbeat
    total_available = 0

    for stay in cfg["stays"]:
        a, d = stay["arrival"], stay["departure"]
        length = stay.get("length_ft", cfg.get("length_ft", 42))
        equip = stay.get("equipment_type_id", cfg.get("equipment_type_id", 3))
        key = f"{a}_{d}_{length}"

        h = fetch(a, d, length, equip)
        avail = available_riverfront(h)
        avail_ids = sorted(avail.keys())
        total_available += len(avail)

        print(f"\n{a} -> {d}  ({nights(a,d)} nt, {length}ft)  "
              f"riverfront available: {len(avail)}")
        for cid, info in avail.items():
            print(f"   AVAILABLE  ${info['price']}/nt  [{cid}] {info['name']}")

        if avail:
            heartbeat_lines.append(f"{pretty(a)}–{pretty(d)}: {len(avail)} open ✅")
        else:
            heartbeat_lines.append(f"{pretty(a)}–{pretty(d)}: none yet")

        # In heartbeat mode we only report; we don't touch state or fire the
        # per-change alert (the 15-min job owns that).
        if heartbeat:
            continue

        prev = set(state.get(key, []))
        now = set(avail_ids)
        newly = now - prev

        if now != prev:
            state[key] = avail_ids
            changed_state = True

        should_push = bool(newly) or (always and now)
        if should_push and avail:
            sites = "\n".join(f"• {i['name']}  (${i['price']}/nt)" for i in avail.values())
            title = f"🏕️ Riverfront open: {pretty(a)}–{pretty(d)}"
            msg = (f"{len(avail)} Riverfront site(s) available for a {length}ft "
                   f"Fifth Wheel:\n{sites}\n\nBook: {BOOK_URL}")
            notify(topic, title, msg)

    if heartbeat:
        summary = "\n".join(heartbeat_lines)
        if total_available:
            title = "🏕️ Daily check — Riverfront sites are OPEN"
            body = f"Monitor is running. Something is available right now:\n{summary}\n\nBook: {BOOK_URL}"
            notify(topic, title, body, tags="tent", priority="default")
        else:
            title = "✅ Daily check — monitor running, nothing yet"
            body = f"Still watching every 15 min. No Riverfront openings found:\n{summary}"
            notify(topic, title, body, tags="hourglass_flowing_sand", priority="low")
        print("\nHeartbeat sent.")
        return

    if changed_state:
        save_state(state)
    print("\nDone.", "State changed." if changed_state else "No change.")


if __name__ == "__main__":
    main(heartbeat=("--heartbeat" in sys.argv))
