#!/usr/bin/env python3
"""
BookMyShow Multi-Date & City-Wide Ticket Tracker v4
----------------------------------------------------
Monitors ALL DATES and ALL THEATRES IN HYDERABAD for:
  - Tickets OPENING (status 0 → 1/2/3 = greyed out → bookable)
  - New bookable showtimes appearing
  - New bookable dates opening
  - New ticket categories becoming bookable

IMPORTANT: BMS availStatus values:
  0 = NOT bookable (greyed out on website, tickets not open yet)
  1 = Bookable (tickets open, available)
  2 = Bookable (fast filling)
  3 = Bookable (almost sold out)

We ONLY notify when something becomes BOOKABLE (status goes from 0 to 1/2/3).
We do NOT spam about shows that are just listed but greyed out.

DESIGN FOR GITHUB ACTIONS:
  - Runs with --once flag (single check per cron invocation)
  - State persisted via bms_state.json committed to git
  - No Playwright dependency (pure API, faster startup)
  - Sends Telegram BEFORE saving state (crash-safe)
"""

import time
import random
import json
import os
import sys
import signal
import requests
from datetime import datetime, timedelta

# ============ CONFIGURATION ============
EVENT_CODE = os.getenv("EVENT_CODE", "ET00505091")
MOVIE_NAME = "Spider-Man: Brand New Day"
BASE_BUY_URL = f"https://in.bookmyshow.com/movies/hyderabad/spider-man-brand-new-day/buytickets/{EVENT_CODE}"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8794059592:AAFdRlSpuRiZYZItgD71DPeO-y6DgNeEaDY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1209851846")

# Generate date codes dynamically: today through today+14 days
def generate_date_codes():
    """Generate date codes for the next 14 days from today."""
    codes = []
    today = datetime.now()
    for i in range(15):
        dt = today + timedelta(days=i)
        codes.append(dt.strftime("%Y%m%d"))
    return codes

# Polling Interval (for continuous/daemon mode only)
CHECK_INTERVAL_MIN = 1.0
CHECK_INTERVAL_MAX = 2.0
MAX_BACKOFF = 30

# File Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "bms_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "tracker.log")

# BMS availability status mapping
# 0 = NOT bookable (greyed out on website)
# 1 = Bookable (available)
# 2 = Bookable (fast filling)
# 3 = Bookable (almost sold out)
STATUS_LABELS = {
    "0": "🔒 Not Open Yet",
    "1": "🟢 Available",
    "2": "🟡 Fast Filling",
    "3": "🔴 Almost Full",
}

def is_bookable(status: str) -> bool:
    """Returns True if the status means tickets are actually open for booking."""
    return str(status) in ("1", "2", "3")

# HTTP Session with retries
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=3)
session.mount("https://", adapter)
session.mount("http://", adapter)

# Global state for signal handler
_current_state_for_signal = None


def log(msg: str):
    """Log to both stdout and file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def send_telegram(message: str) -> bool:
    """Sends HTML formatted Telegram notification. Returns True on success."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = session.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.status_code == 200:
            log(f"✅ Telegram SENT (msg_id: {resp.json().get('result', {}).get('message_id', '?')})")
            return True
        else:
            log(f"❌ Telegram API Error ({resp.status_code}): {resp.text[:200]}")
            return False
    except Exception as e:
        log(f"❌ Telegram Exception: {e}")
        return False


def format_date_display(date_code: str) -> str:
    """Convert 20260730 to '30th July'."""
    try:
        dt_obj = datetime.strptime(date_code, "%Y%m%d")
        day = dt_obj.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"{day}{suffix} {dt_obj.strftime('%B')}"
    except Exception:
        return date_code


def fetch_showtimes_for_date(date_code: str) -> dict:
    """
    Fetch all venue showtimes for a single date via BMS API.
    Returns dict of {venue_name: {venue_code, showtimes: [...]}}
    """
    url = "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "x-app-code": "WEB",
        "x-region-code": "HYD",
        "x-region-slug": "hyderabad",
        "Referer": f"{BASE_BUY_URL}/{date_code}",
    }
    params = {"eventCode": EVENT_CODE, "dateCode": date_code}

    try:
        resp = session.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code != 200:
            log(f"  API HTTP {resp.status_code} for date {date_code}")
            return {}

        data = resp.json().get("data", {})
        widgets = data.get("showtimeWidgets", [])

        venues = {}
        for widget in widgets:
            if widget.get("type") != "groupList":
                continue
            for group in widget.get("data", []):
                for vc in group.get("data", []):
                    add_data = vc.get("additionalData", {})
                    vname = add_data.get("venueName", "Unknown Venue")
                    vcode = add_data.get("venueCode", "")

                    raw_showtimes = list(vc.get("showtimes", []))
                    for sec in vc.get("showtimesSections", []):
                        raw_showtimes.extend(sec.get("showtimes", []))

                    showtimes = []
                    for st in raw_showtimes:
                        st_add = st.get("additionalData", {})
                        session_id = st_add.get("sessionId", "")
                        avail_status = str(st_add.get("availStatus", "0"))
                        show_time = st_add.get("showTime", st.get("title", ""))
                        screen_attr = st_add.get("attributes", "") or st.get("screenAttr", "") or "Standard"

                        categories = []
                        for cat in st_add.get("categories", []):
                            categories.append({
                                "price_desc": cat.get("priceDesc", "Standard"),
                                "price": cat.get("curPrice", "0.00"),
                                "avail_status": str(cat.get("availStatus", "0")),
                            })

                        showtimes.append({
                            "time": show_time,
                            "session_id": session_id,
                            "avail_status": avail_status,
                            "screen_attr": screen_attr,
                            "categories": categories,
                        })

                    if showtimes:
                        venues[vname] = {
                            "venue_code": vcode,
                            "showtimes": showtimes,
                        }

        return venues

    except requests.exceptions.Timeout:
        log(f"  ⏱️ Timeout fetching date {date_code}")
        return {}
    except Exception as e:
        log(f"  ⚠️ Error fetching date {date_code}: {e}")
        return {}


def fetch_all_dates_and_venues() -> dict:
    """Fetch showtimes for ALL dates across ALL venues."""
    date_codes = generate_date_codes()
    log(f"Scanning {len(date_codes)} dates: {date_codes[0]} to {date_codes[-1]}")

    all_data = {}
    total_venues = 0
    total_shows = 0
    bookable_shows = 0

    for dcode in date_codes:
        venues = fetch_showtimes_for_date(dcode)
        booking_url = f"{BASE_BUY_URL}/{dcode}"

        for vname, vinfo in venues.items():
            key = f"{vname}_{dcode}"
            shows = vinfo["showtimes"]
            num_bookable = sum(1 for s in shows if is_bookable(s["avail_status"]))

            all_data[key] = {
                "venue_name": vname,
                "venue_code": vinfo["venue_code"],
                "date_code": dcode,
                "showtimes": shows,
                "target_url": booking_url,
            }
            total_venues += 1
            total_shows += len(shows)
            bookable_shows += num_bookable

    log(f"Scan complete: {total_venues} venue-dates, {total_shows} total shows, {bookable_shows} currently bookable")
    return all_data


def load_previous_state() -> dict:
    """Load previous state from disk."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                log(f"Loaded previous state: {len(state)} venue-date combos")
                return state
        except Exception as e:
            log(f"⚠️ Error loading state file: {e}")
            return {}
    else:
        log(f"No state file found — FIRST RUN")
        return {}


def save_current_state(state: dict):
    """Save state to disk."""
    global _current_state_for_signal
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        _current_state_for_signal = state
        log(f"State saved: {len(state)} venue-date combos")
    except Exception as e:
        log(f"❌ Error saving state: {e}")


def signal_handler(signum, frame):
    """Gracefully handle SIGTERM/SIGINT by saving state before exit."""
    log(f"Received signal {signum}, saving state before exit...")
    if _current_state_for_signal:
        save_current_state(_current_state_for_signal)
    sys.exit(0)


# ============ NOTIFICATION FORMATTERS ============

def format_tickets_opened_alert(venue_name: str, shows: list, date_str: str, booking_url: str) -> str:
    """Alert for when tickets transition from not-bookable to bookable (the main event!)."""
    show_lines = []
    for s in shows:
        attr = s.get("screen_attr", "Standard")
        status = STATUS_LABELS.get(s.get("avail_status", "0"), "❓")
        # Show price info if available
        price_info = ""
        cats = s.get("categories", [])
        bookable_cats = [c for c in cats if is_bookable(c.get("avail_status", "0"))]
        if bookable_cats:
            prices = [f"{c['price_desc']} ₹{c['price']}" for c in bookable_cats]
            price_info = f" — {', '.join(prices)}"
        show_lines.append(f"  ⏰ <b>{s['time']}</b> | {attr} | {status}{price_info}")

    shows_text = "\n".join(show_lines)
    return (
        f"🚨🎟️ <b>TICKETS ARE NOW OPEN!</b> 🎟️🚨\n\n"
        f"🎥 <b>{MOVIE_NAME}</b>\n"
        f"📍 <b>{venue_name}</b>\n"
        f"📅 <b>{date_str}</b>\n\n"
        f"{shows_text}\n\n"
        f"⚡ Book NOW before they sell out!\n"
        f"🔗 <a href=\"{booking_url}\">Book Tickets on BookMyShow</a>"
    )


def format_new_bookable_showtime_alert(venue_name: str, shows: list, date_str: str, booking_url: str) -> str:
    """Alert for new showtimes that are already bookable."""
    show_lines = []
    for s in shows:
        attr = s.get("screen_attr", "Standard")
        status = STATUS_LABELS.get(s.get("avail_status", "0"), "❓")
        show_lines.append(f"  ⏰ <b>{s['time']}</b> | {attr} | {status}")

    shows_text = "\n".join(show_lines)
    return (
        f"🎬 <b>NEW BOOKABLE SHOWS!</b> 🎬\n\n"
        f"🎥 <b>{MOVIE_NAME}</b>\n"
        f"📍 <b>{venue_name}</b>\n"
        f"📅 <b>{date_str}</b>\n\n"
        f"{shows_text}\n\n"
        f"🔗 <a href=\"{booking_url}\">Book Tickets on BookMyShow</a>"
    )


def format_new_date_listed_alert(venue_name: str, date_str: str, num_shows: int, has_bookable: bool, booking_url: str) -> str:
    """Alert for a completely new date appearing (may or may not be bookable yet)."""
    if has_bookable:
        return (
            f"📅🎟️ <b>NEW DATE WITH BOOKABLE SHOWS!</b>\n\n"
            f"🎥 <b>{MOVIE_NAME}</b>\n"
            f"📍 <b>{venue_name}</b>\n"
            f"📅 <b>{date_str}</b> — {num_shows} shows\n\n"
            f"⚡ Tickets are OPEN! Book now!\n"
            f"🔗 <a href=\"{booking_url}\">Book Tickets on BookMyShow</a>"
        )
    else:
        return (
            f"📅 <b>New Date Listed</b>\n\n"
            f"🎥 <b>{MOVIE_NAME}</b>\n"
            f"📍 <b>{venue_name}</b>\n"
            f"📅 <b>{date_str}</b> — {num_shows} shows listed\n"
            f"🔒 Tickets not open yet — you'll be notified when they open!\n\n"
            f"🔗 <a href=\"{booking_url}\">View on BookMyShow</a>"
        )


def format_category_opened_alert(venue_name: str, show_time: str, screen_attr: str,
                                  category_name: str, price: str, date_str: str, booking_url: str) -> str:
    """Alert for a ticket category becoming bookable."""
    return (
        f"🎟️ <b>NEW TICKET CATEGORY OPEN!</b>\n\n"
        f"🎥 <b>{MOVIE_NAME}</b>\n"
        f"📍 <b>{venue_name}</b>\n"
        f"📅 <b>{date_str}</b> | ⏰ <b>{show_time}</b>\n"
        f"🎥 {screen_attr}\n"
        f"💺 <b>{category_name}</b> — ₹{price} — Now Bookable!\n\n"
        f"🔗 <a href=\"{booking_url}\">Book Tickets on BookMyShow</a>"
    )


# ============ MAIN DETECTION LOGIC ============

def run_check_cycle(previous_state: dict) -> dict:
    """
    Main detection logic. Compares current API data against previous state.
    Only notifies for MEANINGFUL changes (tickets becoming bookable).
    """
    current_state = fetch_all_dates_and_venues()

    if not current_state:
        log("⚠️ API returned 0 venues/dates. Keeping previous state.")
        return previous_state

    # FIRST RUN: Establish baseline, send summary
    if not previous_state:
        log(f"🆕 FIRST RUN: Establishing baseline with {len(current_state)} venue-date combos")

        venue_names = set()
        date_codes = set()
        total_shows = 0
        bookable = 0
        for key, vdata in current_state.items():
            venue_names.add(vdata["venue_name"])
            date_codes.add(vdata["date_code"])
            for s in vdata.get("showtimes", []):
                total_shows += 1
                if is_bookable(s["avail_status"]):
                    bookable += 1

        dates_display = ", ".join(sorted(format_date_display(d) for d in date_codes))

        startup_msg = (
            f"✅ <b>BMS Tracker v4 Started!</b>\n\n"
            f"🎥 <b>{MOVIE_NAME}</b>\n"
            f"📍 {len(venue_names)} venue(s): {', '.join(sorted(venue_names))}\n"
            f"📅 Dates: {dates_display}\n"
            f"🎟️ {total_shows} shows tracked ({bookable} currently bookable)\n\n"
            f"You'll be notified ONLY when tickets actually OPEN for booking!"
        )
        send_telegram(startup_msg)
        save_current_state(current_state)
        return current_state

    # ============ COMPARE & DETECT ============
    notifications_sent = 0

    for key, vdata in current_state.items():
        vname = vdata["venue_name"]
        dcode = vdata["date_code"]
        date_display = format_date_display(dcode)
        booking_url = vdata.get("target_url", BASE_BUY_URL)
        curr_shows = vdata.get("showtimes", [])

        # ---- Case 1: Entirely new venue-date combo ----
        if key not in previous_state:
            bookable_shows = [s for s in curr_shows if is_bookable(s["avail_status"])]
            has_bookable = len(bookable_shows) > 0

            if has_bookable:
                # New date with tickets already open — HIGH PRIORITY
                log(f"🆕🎟️ NEW DATE WITH BOOKABLE SHOWS: {vname} on {date_display} ({len(bookable_shows)} bookable)")
                msg = format_new_date_listed_alert(vname, date_display, len(curr_shows), True, booking_url)
                if send_telegram(msg):
                    notifications_sent += 1
            else:
                # New date listed but nothing bookable yet — LOW PRIORITY, still notify but calmer
                log(f"📅 New date listed (not bookable yet): {vname} on {date_display} ({len(curr_shows)} shows)")
                msg = format_new_date_listed_alert(vname, date_display, len(curr_shows), False, booking_url)
                if send_telegram(msg):
                    notifications_sent += 1
            continue

        # ---- Case 2: Existing venue-date — detect changes ----
        prev_shows_map = {s["session_id"]: s for s in previous_state[key].get("showtimes", []) if s.get("session_id")}

        # Detect new showtimes that didn't exist before
        new_shows = [s for s in curr_shows if s.get("session_id") and s["session_id"] not in prev_shows_map]
        if new_shows:
            bookable_new = [s for s in new_shows if is_bookable(s["avail_status"])]
            if bookable_new:
                log(f"🆕🎟️ NEW BOOKABLE SHOWTIMES at {vname} on {date_display}: {[s['time'] for s in bookable_new]}")
                msg = format_new_bookable_showtime_alert(vname, bookable_new, date_display, booking_url)
                if send_telegram(msg):
                    notifications_sent += 1
            else:
                log(f"📝 New shows listed (not bookable yet) at {vname} on {date_display}: {[s['time'] for s in new_shows]}")
                # Don't spam about non-bookable new shows

        # Detect tickets OPENING (status 0 → 1/2/3) — THE MOST IMPORTANT EVENT
        newly_opened = []
        for curr_s in curr_shows:
            sid = curr_s.get("session_id", "")
            if not sid or sid not in prev_shows_map:
                continue

            prev_s = prev_shows_map[sid]
            old_avail = str(prev_s.get("avail_status", "0"))
            new_avail = str(curr_s.get("avail_status", "0"))

            # Tickets just opened! (was greyed out, now bookable)
            if not is_bookable(old_avail) and is_bookable(new_avail):
                newly_opened.append(curr_s)

            # Category-level: a specific ticket category just became bookable
            prev_cats = {c["price_desc"]: c for c in prev_s.get("categories", [])}
            for curr_c in curr_s.get("categories", []):
                cname = curr_c["price_desc"]
                curr_cat_status = str(curr_c.get("avail_status", "0"))
                prev_cat = prev_cats.get(cname)

                if prev_cat and not is_bookable(str(prev_cat.get("avail_status", "0"))) and is_bookable(curr_cat_status):
                    # Only notify category if the overall show was already bookable
                    # (to avoid double-notifying when the whole show just opened)
                    if is_bookable(old_avail):
                        log(f"🎟️ CATEGORY OPENED: {cname} at {vname} ({date_display}, {curr_s['time']})")
                        msg = format_category_opened_alert(
                            vname, curr_s["time"], curr_s.get("screen_attr", "Standard"),
                            cname, curr_c.get("price", "?"), date_display, booking_url
                        )
                        if send_telegram(msg):
                            notifications_sent += 1

        if newly_opened:
            log(f"🚨 TICKETS OPENED at {vname} on {date_display}: {[s['time'] for s in newly_opened]}")
            msg = format_tickets_opened_alert(vname, newly_opened, date_display, booking_url)
            if send_telegram(msg):
                notifications_sent += 1

    if notifications_sent > 0:
        log(f"📨 Sent {notifications_sent} notifications this cycle")
    else:
        log("✓ No bookable changes detected.")

    # Save state AFTER all notifications (crash-safe)
    save_current_state(current_state)
    return current_state


def main():
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    single_run = "--once" in sys.argv

    log("=" * 60)
    log(f"BMS Ticket Tracker v4 — {'SINGLE CHECK' if single_run else 'CONTINUOUS DAEMON'}")
    log(f"Movie: {MOVIE_NAME} | Event: {EVENT_CODE}")
    log(f"State File: {STATE_FILE} (exists: {os.path.exists(STATE_FILE)})")
    log("=" * 60)

    previous_state = load_previous_state()

    if single_run:
        run_check_cycle(previous_state)
        log("Single check complete.")
        return

    # Continuous daemon mode (local only)
    consecutive_errors = 0
    while True:
        try:
            previous_state = run_check_cycle(previous_state)
            consecutive_errors = 0
            time.sleep(random.uniform(CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX))
        except KeyboardInterrupt:
            log("Stopped by user.")
            if previous_state:
                save_current_state(previous_state)
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"⚠️ Error ({consecutive_errors}): {e}")
            time.sleep(min(CHECK_INTERVAL_MAX * (1.5 ** consecutive_errors), MAX_BACKOFF))


if __name__ == "__main__":
    main()
