#!/usr/bin/env python3
"""
BookMyShow Multi-Date & City-Wide Ticket Tracker v3
----------------------------------------------------
Monitors ALL DATES and ALL THEATRES IN HYDERABAD for:
  - New show releases
  - New dates opening
  - Seat/row unblocks (status changes)

DESIGN FOR GITHUB ACTIONS:
  - Runs with --once flag (single check per cron invocation)
  - State persisted via bms_state.json committed to git
  - No Playwright dependency (pure API, faster startup)
  - Sends Telegram BEFORE saving state (crash-safe)

USAGE:
    Single Check (GitHub Actions - recommended):
        python3 bms_tracker.py --once

    Continuous Daemon (local only):
        python3 bms_tracker.py
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
    print(line, flush=True)  # flush=True ensures GitHub Actions shows logs in real-time
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
            log(f"✅ Telegram notification SENT successfully (msg_id: {resp.json().get('result', {}).get('message_id', '?')})")
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
        return f"{day}{suffix} {dt_obj.strftime('%B %Y')}"
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

                    # Collect showtimes from both flat list and sections
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
        log(f"  ⏱️ Timeout fetching date {date_code} (20s)")
        return {}
    except Exception as e:
        log(f"  ⚠️ Error fetching date {date_code}: {e}")
        return {}


def fetch_all_dates_and_venues() -> dict:
    """
    Fetch showtimes for ALL dates across ALL venues.
    Returns dict keyed by 'VenueName_DateCode'.
    """
    date_codes = generate_date_codes()
    log(f"Scanning {len(date_codes)} dates: {date_codes[0]} to {date_codes[-1]}")

    all_data = {}
    total_venues = 0
    total_shows = 0

    for dcode in date_codes:
        venues = fetch_showtimes_for_date(dcode)
        booking_url = f"{BASE_BUY_URL}/{dcode}"

        for vname, vinfo in venues.items():
            key = f"{vname}_{dcode}"
            num_shows = len(vinfo["showtimes"])
            all_data[key] = {
                "venue_name": vname,
                "venue_code": vinfo["venue_code"],
                "date_code": dcode,
                "showtimes": vinfo["showtimes"],
                "target_url": booking_url,
            }
            total_venues += 1
            total_shows += num_shows

    log(f"Scan complete: {total_venues} venue-date combos, {total_shows} total showtimes across {len(date_codes)} dates")
    return all_data


def load_previous_state() -> dict:
    """Load previous state from disk."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                log(f"Loaded previous state: {len(state)} venue-date combos from {STATE_FILE}")
                return state
        except Exception as e:
            log(f"⚠️ Error loading state file: {e}")
            return {}
    else:
        log(f"No state file found at {STATE_FILE} — this is a FIRST RUN")
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


def format_new_showtime_alert(venue_name: str, showtimes: list, date_str: str, booking_url: str) -> str:
    show_lines = []
    for s in showtimes:
        attr = s.get("screen_attr", "Standard")
        status_map = {"0": "🔒 Coming Soon", "1": "🟢 Available", "2": "🟡 Filling Fast", "3": "🔴 Almost Full"}
        status = status_map.get(s.get("avail_status", "0"), "❓ Unknown")
        show_lines.append(f"  ⏰ <b>{s['time']}</b> | {attr} | {status}")

    shows_text = "\n".join(show_lines)
    return (
        f"🎬 <b>NEW SHOWS DETECTED!</b> 🎬\n\n"
        f"🎥 <b>{MOVIE_NAME}</b>\n"
        f"📍 <b>{venue_name}</b>\n"
        f"📅 <b>{date_str}</b>\n\n"
        f"{shows_text}\n\n"
        f"🔗 <a href=\"{booking_url}\">Book Tickets Now on BookMyShow</a>"
    )


def format_status_change_alert(venue_name: str, show_time: str, screen_attr: str,
                                date_str: str, old_status: str, new_status: str,
                                booking_url: str) -> str:
    status_map = {"0": "🔒 Coming Soon", "1": "🟢 Available", "2": "🟡 Filling Fast", "3": "🔴 Almost Full"}
    old_label = status_map.get(old_status, old_status)
    new_label = status_map.get(new_status, new_status)

    return (
        f"🔥 <b>TICKETS STATUS CHANGED!</b> 🔥\n\n"
        f"🎥 <b>{MOVIE_NAME}</b>\n"
        f"📍 <b>{venue_name}</b>\n"
        f"📅 <b>{date_str}</b>\n"
        f"⏰ <b>{show_time}</b> | {screen_attr}\n\n"
        f"📊 {old_label} ➡️ {new_label}\n\n"
        f"🔗 <a href=\"{booking_url}\">Book Tickets Now on BookMyShow</a>"
    )


def format_category_unblock_alert(venue_name: str, show_time: str, screen_attr: str,
                                   category_name: str, date_str: str, booking_url: str) -> str:
    return (
        f"🎟️ <b>NEW TICKET CATEGORY AVAILABLE!</b> 🎟️\n\n"
        f"🎥 <b>{MOVIE_NAME}</b>\n"
        f"📍 <b>{venue_name}</b>\n"
        f"📅 <b>{date_str}</b>\n"
        f"⏰ <b>{show_time}</b> | {screen_attr}\n"
        f"💺 <b>{category_name}</b> — Now Bookable!\n\n"
        f"🔗 <a href=\"{booking_url}\">Book Tickets Now on BookMyShow</a>"
    )


def run_check_cycle(previous_state: dict) -> dict:
    """
    Main detection logic. Compares current API data against previous state.
    Sends Telegram for every change BEFORE saving state (crash-safe).
    """
    current_state = fetch_all_dates_and_venues()

    if not current_state:
        log("⚠️ API returned 0 venues/dates. Possible API issue or network problem. Keeping previous state.")
        return previous_state

    # FIRST RUN: No previous state at all — establish baseline
    if not previous_state:
        log(f"🆕 FIRST RUN: Establishing baseline with {len(current_state)} venue-date combos")
        # Send a summary notification so user knows tracking started
        venue_names = set()
        date_codes = set()
        total_shows = 0
        for key, vdata in current_state.items():
            venue_names.add(vdata["venue_name"])
            date_codes.add(vdata["date_code"])
            total_shows += len(vdata.get("showtimes", []))

        dates_display = ", ".join(sorted(format_date_display(d) for d in date_codes))
        venues_display = ", ".join(sorted(venue_names))

        startup_msg = (
            f"✅ <b>BMS Tracker Started Successfully!</b>\n\n"
            f"🎥 Monitoring: <b>{MOVIE_NAME}</b>\n"
            f"📍 Venues: {venues_display}\n"
            f"📅 Dates: {dates_display}\n"
            f"🎟️ Total showtimes tracked: <b>{total_shows}</b>\n\n"
            f"You will be notified instantly when new shows open or ticket status changes!"
        )
        send_telegram(startup_msg)
        save_current_state(current_state)
        return current_state

    # COMPARE: Detect changes
    notifications_sent = 0

    for key, vdata in current_state.items():
        vname = vdata["venue_name"]
        dcode = vdata["date_code"]
        date_display = format_date_display(dcode)
        booking_url = vdata.get("target_url", BASE_BUY_URL)
        curr_shows = vdata.get("showtimes", [])

        # Case 1: Entirely new venue-date combo (new theatre or new date opened)
        if key not in previous_state:
            log(f"🆕 NEW VENUE/DATE: {vname} on {date_display} ({len(curr_shows)} shows)")
            msg = format_new_showtime_alert(vname, curr_shows, date_display, booking_url)
            if send_telegram(msg):
                notifications_sent += 1
            continue

        # Case 2: Existing venue-date — check for new showtimes and status changes
        prev_shows_map = {s["session_id"]: s for s in previous_state[key].get("showtimes", []) if s.get("session_id")}

        # Detect new showtimes (session_id not in previous)
        new_shows = [s for s in curr_shows if s.get("session_id") and s["session_id"] not in prev_shows_map]
        if new_shows:
            log(f"🆕 NEW SHOWTIMES at {vname} on {date_display}: {[s['time'] for s in new_shows]}")
            msg = format_new_showtime_alert(vname, new_shows, date_display, booking_url)
            if send_telegram(msg):
                notifications_sent += 1

        # Detect status changes for existing showtimes
        for curr_s in curr_shows:
            sid = curr_s.get("session_id", "")
            if not sid or sid not in prev_shows_map:
                continue

            prev_s = prev_shows_map[sid]

            # Overall availability status change (e.g., 0->1 means tickets just opened)
            old_avail = str(prev_s.get("avail_status", "0"))
            new_avail = str(curr_s.get("avail_status", "0"))

            if old_avail != new_avail:
                # Only notify for meaningful changes:
                # 0->1,2,3 = tickets just opened (most important!)
                # Any change is worth reporting
                log(f"📊 STATUS CHANGE at {vname} ({date_display}, {curr_s['time']}): {old_avail} -> {new_avail}")
                msg = format_status_change_alert(
                    vname, curr_s["time"], curr_s.get("screen_attr", "Standard"),
                    date_display, old_avail, new_avail, booking_url
                )
                if send_telegram(msg):
                    notifications_sent += 1

            # Category-level unblock detection
            prev_cats = {c["price_desc"]: str(c.get("avail_status", "0")) for c in prev_s.get("categories", [])}
            for curr_c in curr_s.get("categories", []):
                cname = curr_c["price_desc"]
                curr_cat_status = str(curr_c.get("avail_status", "0"))
                prev_cat_status = prev_cats.get(cname, None)

                if prev_cat_status is not None and prev_cat_status == "0" and curr_cat_status != "0":
                    log(f"🎟️ CATEGORY UNBLOCKED: {cname} at {vname} ({date_display}, {curr_s['time']})")
                    msg = format_category_unblock_alert(
                        vname, curr_s["time"], curr_s.get("screen_attr", "Standard"),
                        cname, date_display, booking_url
                    )
                    if send_telegram(msg):
                        notifications_sent += 1

    if notifications_sent > 0:
        log(f"📨 Total notifications sent this cycle: {notifications_sent}")
    else:
        log("No changes detected this cycle.")

    # Save state AFTER all notifications sent (crash-safe: if we crash before save,
    # next run will re-detect and re-notify — better to double-notify than miss)
    save_current_state(current_state)
    return current_state


def main():
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    single_run = "--once" in sys.argv

    log("=" * 60)
    log(f"BMS Ticket Tracker v3 — {'SINGLE CHECK' if single_run else 'CONTINUOUS DAEMON'}")
    log(f"Movie: {MOVIE_NAME} | Event: {EVENT_CODE}")
    log(f"Telegram Bot: {TELEGRAM_BOT_TOKEN[:10]}... | Chat: {TELEGRAM_CHAT_ID}")
    log(f"State File: {STATE_FILE}")
    log(f"State File Exists: {os.path.exists(STATE_FILE)}")
    log("=" * 60)

    previous_state = load_previous_state()

    if single_run:
        # GitHub Actions mode: single check, then exit
        run_check_cycle(previous_state)
        log("Single check complete. Exiting.")
        return

    # Continuous daemon mode (local only)
    consecutive_errors = 0
    while True:
        try:
            previous_state = run_check_cycle(previous_state)
            consecutive_errors = 0
            interval = random.uniform(CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX)
            time.sleep(interval)
        except KeyboardInterrupt:
            log("Tracker stopped by user.")
            if previous_state:
                save_current_state(previous_state)
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"⚠️ Error in cycle ({consecutive_errors}): {e}")
            backoff = min(CHECK_INTERVAL_MAX * (1.5 ** consecutive_errors), MAX_BACKOFF)
            time.sleep(backoff)


if __name__ == "__main__":
    main()
