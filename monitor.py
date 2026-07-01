#!/usr/bin/env python3
"""
Edgewater @ Lava Hot Springs — Riverfront campsite availability monitor.

For each configured stay it evaluates two outcomes and pushes an ntfy alert
(with site names + dates) when either newly becomes true:

  1. FULL  — a single RIVERFRONT site is bookable for the entire stay
             (a real "Book now" on the multi-night search). Best case.
  2. SPLIT — no single Riverfront site covers the whole stay, but by moving
             sites mid-stay you can spend at least `min_riverfront_nights`
             of the nights on Riverfront, with every night bookable somewhere.
             (Matches the site's "I'm willing to move mid-stay" option.)

The split analysis is done by checking each night individually — the same
per-night availability the site itself stitches together — which is far more
robust than scraping the combination UI.

Browser-free: plain HTTPS requests. Cloudflare's __cf_bm check is satisfied by
a normal browser User-Agent + one warm-up GET (cookies persist in COOKIE_JAR).

Config: config.json.  State (notify-on-change): state.json.
ntfy topic: NTFY_TOPIC env var (falls back to config.json "ntfy_topic").
Run `python3 monitor.py --heartbeat` for the daily "still running" summary.
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

_warmed = False


def curl(url, data=None, load_cookies=False):
    cmd = ["curl", "-sS", "--max-time", "45", "-A", UA,
           "-H", "X-Requested-With: XMLHttpRequest", "-c", COOKIE_JAR]
    if load_cookies:
        cmd += ["-b", COOKIE_JAR]
    if data:
        for k, v in data.items():
            cmd += ["--data-urlencode", f"{k}={v}"]
    cmd += [url]
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def warm_up():
    global _warmed
    if not _warmed:
        curl(PAGE)  # establish cf_bm + PHPSESSID once per process
        _warmed = True


def pretty(d):                       # 'YYYY-MM-DD' -> 'August 14 2026'
    dt = datetime.date.fromisoformat(d)
    return f"{calendar.month_name[dt.month]} {dt.day} {dt.year}"


def short(d):                        # 'YYYY-MM-DD' -> 'Fri Aug 14'
    dt = datetime.date.fromisoformat(d)
    return dt.strftime("%a %b %-d")


def nights(a, b):
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


def night_ranges(arrival, departure):
    """['2026-08-14'..'2026-08-17'] -> [(14,15),(15,16),(16,17)] as ISO strings."""
    d0 = datetime.date.fromisoformat(arrival)
    out = []
    for i in range(nights(arrival, departure)):
        a = d0 + datetime.timedelta(days=i)
        out.append((a.isoformat(), (a + datetime.timedelta(days=1)).isoformat()))
    return out


def fetch(arrival, departure, length, equip_type):
    warm_up()
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


def bookable(h):
    """All categories with a real 'Book now' button for the searched period.
    Returns {category_id: {'name','price','riverfront'}}. The site renders one
    hidden Book-now button per available category, tagged with its id/name."""
    out = {}
    for m in re.finditer(r"<button([^>]*aria-label='Book now'[^>]*)>", h):
        attrs = m.group(1)
        cid = (re.search(r"category_id='(\d+)'", attrs) or [None, None])[1]
        cname = html.unescape((re.search(r"category_name='([^']*)'", attrs) or [None, ''])[1])
        amt = (re.search(r"full_amount='([\d.]+)'", attrs) or [None, ''])[1]
        if cid:
            out.setdefault(cid, {"name": cname, "price": amt,
                                 "riverfront": "riverfront" in cname.lower()})
    return out


def riverfront_only(avail):
    return {c: v for c, v in avail.items() if v["riverfront"]}


def price(v):
    try:
        return f"${float(v['price']):.0f}/nt"
    except Exception:
        return ""


def evaluate(arrival, departure, length, equip, min_river):
    """Return an outcome dict describing the best bookable result for the stay."""
    total = nights(arrival, departure)

    # --- FULL: single riverfront site for the whole stay ---
    full = riverfront_only(bookable(fetch(arrival, departure, length, equip)))

    # --- SPLIT: per-night decomposition (only needed if no full riverfront) ---
    per_night = []
    if total > 1 and not full:
        for a, b in night_ranges(arrival, departure):
            av = bookable(fetch(a, b, length, equip))
            river = riverfront_only(av)
            pick = (next(iter(river.values())) if river
                    else (next(iter(av.values())) if av else None))
            per_night.append({
                "from": a, "to": b,
                "any": bool(av),
                "riverfront": bool(river),
                "site": pick,  # chosen site for this night (riverfront preferred)
            })

    river_nights = sum(1 for n in per_night if n["riverfront"])
    all_bookable = bool(per_night) and all(n["any"] for n in per_night)
    split_ok = all_bookable and river_nights >= min_river

    if full:
        tier = "full"
        sig = "full:" + ",".join(sorted(full))
    elif split_ok:
        tier = "split"
        sig = "split:" + "".join("R" if n["riverfront"] else "-" for n in per_night)
    else:
        tier = None
        sig = "none"

    return {
        "arrival": arrival, "departure": departure, "total": total,
        "length": length, "min_river": min_river,
        "full": full, "per_night": per_night,
        "river_nights": river_nights, "tier": tier, "signature": sig,
    }


def build_message(o):
    a, d = o["arrival"], o["departure"]
    span = f"{short(a)} → {short(d)}"
    if o["tier"] == "full":
        sites = "\n".join(f"• {v['name']} — {price(v)}" for v in o["full"].values())
        title = f"🏕️ FULL Riverfront stay open: {span}"
        body = (f"A single Riverfront site is bookable for the whole "
                f"{o['total']}-night stay ({span}, {o['length']}ft Fifth Wheel):\n"
                f"{sites}\n\nBook: {BOOK_URL}")
        return title, body, "tent,ocean", "high"
    # split
    lines = []
    for n in o["per_night"]:
        tag = "✅ Riverfront" if n["riverfront"] else "· non-riverfront"
        s = n["site"]
        name = s["name"] if s else "(none)"
        to_day = datetime.date.fromisoformat(n["to"]).day
        lines.append(f"• {short(n['from'])}→{to_day}: {name} {price(s) if s else ''}  {tag}")
    body_lines = "\n".join(lines)
    title = f"🏕️ Move-stay: {o['river_nights']}/{o['total']} Riverfront nights — {span}"
    body = (f"Whole stay is bookable if you move sites mid-stay, with "
            f"{o['river_nights']} of {o['total']} nights on Riverfront "
            f"({o['length']}ft Fifth Wheel):\n{body_lines}\n\nBook: {BOOK_URL}")
    return title, body, "tent", "high"


def notify(topic, title, message, tags="tent", priority="high"):
    if not topic:
        print("  [notify] NTFY_TOPIC not set — skipping push")
        return
    cmd = ["curl", "-sS", "--max-time", "20",
           "-H", f"Title: {title}", "-H", f"Priority: {priority}",
           "-H", f"Tags: {tags}", "-H", f"Click: {BOOK_URL}",
           "-d", message, f"https://ntfy.sh/{topic}"]
    subprocess.run(cmd, capture_output=True, text=True)
    print(f"  [notify] pushed to ntfy topic '{topic}'")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def default_min_river(total):
    """Strict majority of nights, e.g. 3 nights -> 2, 4 -> 3, 2 -> 2, 1 -> 1."""
    return total // 2 + 1


def main(heartbeat=False):
    cfg = json.loads((HERE / "config.json").read_text())
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    max_notifs = int(cfg.get("max_notifications", 4))
    state = load_state()
    changed_state = False
    hb_lines = []
    any_hit = False

    for stay in cfg["stays"]:
        a, d = stay["arrival"], stay["departure"]
        length = stay.get("length_ft", cfg.get("length_ft", 42))
        equip = stay.get("equipment_type_id", cfg.get("equipment_type_id", 3))
        total = nights(a, d)
        min_river = stay.get("min_riverfront_nights",
                             cfg.get("min_riverfront_nights", default_min_river(total)))
        key = f"{a}_{d}_{length}"

        o = evaluate(a, d, length, equip, min_river)

        # log
        if o["tier"] == "full":
            desc = "FULL riverfront (" + ", ".join(v["name"] for v in o["full"].values()) + ")"
        elif o["tier"] == "split":
            desc = f"SPLIT {o['river_nights']}/{total} riverfront nights"
        else:
            rn = o["river_nights"]
            desc = (f"no qualifying option ({rn}/{total} riverfront nights, "
                    f"need {min_river})") if o["per_night"] else "no full riverfront"
        print(f"\n{a} -> {d} ({total} nt, {length}ft) [need >={min_river} riverfront]: {desc}")

        if o["tier"]:
            any_hit = True
            hb_lines.append(f"{short(a)}–{short(d)}: {desc}")
        else:
            hb_lines.append(f"{short(a)}–{short(d)}: none yet")

        if heartbeat:
            continue

        # Per-stay state: {signature, count}. `count` = how many times we've
        # already pushed for THIS availability window. A new/changed signature
        # (sites filling and reopening, or a different set of sites) resets it.
        prev = state.get(key)
        prev = prev if isinstance(prev, dict) else {}
        prev_sig, prev_count = prev.get("signature"), prev.get("count", 0)
        count = prev_count if o["signature"] == prev_sig else 0

        if o["tier"] and count < max_notifs:
            title, body, tags, prio = build_message(o)
            notify(topic, title, body, tags, prio)
            count += 1
            print(f"  [notify] {count}/{max_notifs} for this availability window")
        elif o["tier"]:
            print(f"  [notify] cap reached ({max_notifs}); silent until availability changes")

        new_entry = {"signature": o["signature"], "count": count}
        if new_entry != prev:
            state[key] = new_entry
            changed_state = True

    if heartbeat:
        summary = "\n".join(hb_lines)
        if any_hit:
            notify(topic, "🏕️ Daily check — Riverfront option available!",
                   f"Monitor running. Something qualifies right now:\n{summary}\n\nBook: {BOOK_URL}",
                   tags="tent", priority="default")
        else:
            notify(topic, "✅ Daily check — monitor running, nothing yet",
                   f"Still checking every 15 min. No qualifying Riverfront options:\n{summary}",
                   tags="hourglass_flowing_sand", priority="low")
        print("\nHeartbeat sent.")
        return

    if changed_state:
        save_state(state)
    print("\nDone.", "State changed." if changed_state else "No change.")


if __name__ == "__main__":
    main(heartbeat=("--heartbeat" in sys.argv))
