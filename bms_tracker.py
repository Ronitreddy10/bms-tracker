#!/usr/bin/env python3
"""
BookMyShow Multi-Format High-Speed Ticket & Row Unblock Tracker
----------------------------------------------------------------
Monitors ALL screen formats (Dolby Atmos, 3D, 2D, IMAX, 4DX, PCX HDR) across ALL venues
at high frequency (1-2 second polling interval) for instant notifications.

USAGE:
    High-Speed Local Daemon (1-2 sec check interval):
        python3 bms_tracker.py

    Single Check Mode (GitHub Actions):
        python3 bms_tracker.py --once
"""

import time
import random
import json
import os
import sys
import re
import requests
from datetime import datetime
from urllib.parse import urlparse

# ============ CONFIGURATION ============
# Support comma-separated URLs or single URL to monitor all formats (2D, 3D, Dolby Atmos, IMAX, etc.)
DEFAULT_URLS = [
    "https://in.bookmyshow.com/movies/hyderabad/spider-man-brand-new-day/buytickets/ET00505091/20260730",
    "https://in.bookmyshow.com/movies/hyderabad/spider-man-brand-new-day/buytickets/ET00447840/20260730"
]

BMS_URL_ENV = os.getenv("BMS_URL", "")
if BMS_URL_ENV:
    BMS_URLS = [u.strip() for u in BMS_URL_ENV.split(",") if u.strip()]
else:
    BMS_URLS = DEFAULT_URLS

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8794059592:AAFdRlSpuRiZYZItgD71DPeO-y6DgNeEaDY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1209851846")

# High speed polling interval
CHECK_INTERVAL_MIN = 1.0
CHECK_INTERVAL_MAX = 2.0
MAX_BACKOFF = 30

# File Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "bms_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "tracker.log")

# Region resolution mapping
REGION_MAP = {
    "chennai":    ("CHEN",   "chennai"),
    "mumbai":     ("MUMBAI", "mumbai"),
    "delhi-ncr":  ("NCR",    "delhi-ncr"),
    "delhi":      ("NCR",    "delhi-ncr"),
    "bengaluru":  ("BANG",   "bengaluru"),
    "bangalore":  ("BANG",   "bengaluru"),
    "hyderabad":  ("HYD",    "hyderabad"),
    "kolkata":    ("KOLK",   "kolkata"),
    "pune":       ("PUNE",   "pune"),
    "kochi":      ("KOCH",   "kochi"),
}

# Re-use HTTP Session for maximum speed (<100ms request latency)
session = requests.Session()


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def send_telegram(message: str):
    """Sends HTML formatted Telegram notification."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = session.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if resp.status_code != 200:
            log(f"Telegram API Error ({resp.status_code}): {resp.text}")
        else:
            log("Telegram notification sent successfully.")
    except Exception as e:
        log(f"Telegram Exception: {e}")


def parse_url_info(url):
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    
    event_code = "ET00505091"
    date_code = "20260730"
    region_slug = "hyderabad"
    
    for part in path_parts:
        if re.match(r"^ET\d{8,}$", part):
            event_code = part
        elif re.match(r"^\d{8}$", part):
            date_code = part

    if "movies" in path_parts:
        idx = path_parts.index("movies")
        if idx + 1 < len(path_parts):
            region_slug = path_parts[idx + 1]
            
    region_code = REGION_MAP.get(region_slug.lower(), (region_slug.upper()[:4], region_slug))[0]
    return event_code, date_code, region_code, region_slug, url


def fetch_showtimes_for_url(url):
    event_code, date_code, region_code, region_slug, target_url = parse_url_info(url)
    
    api_endpoint = "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "Referer": target_url
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code
    }
    
    response = session.get(api_endpoint, headers=headers, params=params, timeout=5)
    if response.status_code != 200:
        raise ValueError(f"BMS API HTTP {response.status_code} for {event_code}")
        
    res_json = response.json()
    data = res_json.get("data", {})
    widgets = data.get("showtimeWidgets", [])
    
    venues = {}
    for widget in widgets:
        if widget.get("type") == "groupList":
            for group in widget.get("data", []):
                for vc in group.get("data", []):
                    add_data = vc.get("additionalData", {})
                    vname = add_data.get("venueName", "Unknown Venue")
                    vcode = add_data.get("venueCode", "")
                    
                    showtimes = []
                    for st in vc.get("showtimes", []):
                        st_title = st.get("title", "")
                        st_add = st.get("additionalData", {})
                        session_id = st_add.get("sessionId", "")
                        avail_status = st_add.get("availStatus", "0")
                        show_time_code = st_add.get("showTime", st_title)
                        screen_attr = st_add.get("attributes", st.get("screenAttr", "")) or "Standard"
                        
                        categories = []
                        for cat in st_add.get("categories", []):
                            categories.append({
                                "price_desc": cat.get("priceDesc", "Standard"),
                                "price": cat.get("curPrice", "0.00"),
                                "avail_status": cat.get("availStatus", "0")
                            })
                            
                        showtimes.append({
                            "time": show_time_code,
                            "session_id": session_id,
                            "avail_status": avail_status,
                            "screen_attr": screen_attr,
                            "categories": categories,
                            "target_url": target_url
                        })
                        
                    key = f"{vname}_{event_code}"
                    venues[key] = {
                        "venue_name": vname,
                        "venue_code": vcode,
                        "event_code": event_code,
                        "showtimes": showtimes,
                        "target_url": target_url
                    }
                    
    return venues


def fetch_all_formats():
    combined = {}
    for url in BMS_URLS:
        try:
            v_dict = fetch_showtimes_for_url(url)
            combined.update(v_dict)
        except Exception as e:
            log(f"Error fetching URL ({url}): {e}")
    return combined


def load_previous_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_current_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"Error saving state: {e}")


def format_row_unblock_alert(row_name: str, movie_hashtag: str, venue_name: str, showtime: str, screen_attr: str, date_str: str, booking_url: str):
    screen_line = f"🎥 <b>Format:</b> {screen_attr}\n" if screen_attr else ""
    return (
        f"🔥 <b>{row_name} rows unblocked</b> for {movie_hashtag} 🍿 at <b>{venue_name}</b>.\n\n"
        f"{screen_line}"
        f"📅 <b>Date & Time:</b> {date_str}, {showtime}\n"
        f"🎟️ <b>Status:</b> Tickets / Rows Available Now!\n\n"
        f"🔗 <a href=\"{booking_url}\">Book Tickets Now on BookMyShow</a>"
    )


def format_new_showtime_alert(venue_name: str, showtimes: list, date_str: str, booking_url: str):
    show_list = ", ".join([f"<b>{s['time']}</b> ({s.get('screen_attr', 'Standard')})" for s in showtimes])
    return (
        f"🎬 <b>New Showtimes Released!</b>\n\n"
        f"📍 <b>{venue_name}</b>\n"
        f"📅 <b>Date:</b> {date_str}\n"
        f"⏰ <b>Shows:</b> {show_list}\n\n"
        f"🔗 <a href=\"{booking_url}\">Book Tickets on BookMyShow</a>"
    )


def run_check_cycle(previous_state):
    # Obtain date code from first URL
    _, date_code, _, _, _ = parse_url_info(BMS_URLS[0])
    
    try:
        dt_obj = datetime.strptime(date_code, "%Y%m%d")
        day = dt_obj.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        date_display = f"{day}{suffix} {dt_obj.strftime('%B')}"
    except Exception:
        date_display = date_code

    current_state = fetch_all_formats()
    
    if not previous_state:
        log("Initial state baseline saved.")
        save_current_state(current_state)
        return current_state
        
    for vkey, vdata in current_state.items():
        vname = vdata["venue_name"]
        booking_url = vdata.get("target_url", BMS_URLS[0])
        
        if vkey not in previous_state:
            log(f"NEW THEATRE / FORMAT OPENED: {vname}")
            msg = format_new_showtime_alert(vname, vdata.get("showtimes", []), date_display, booking_url)
            send_telegram(msg)
        else:
            prev_shows = {s["session_id"]: s for s in previous_state[vkey].get("showtimes", [])}
            curr_shows = vdata.get("showtimes", [])
            
            new_shows = [s for s in curr_shows if s["session_id"] not in prev_shows]
            if new_shows:
                log(f"NEW SHOWTIMES at {vname}: {[s['time'] for s in new_shows]}")
                msg = format_new_showtime_alert(vname, new_shows, date_display, booking_url)
                send_telegram(msg)
                
            for curr_s in curr_shows:
                sid = curr_s["session_id"]
                s_attr = curr_s.get("screen_attr", "")
                if sid in prev_shows:
                    prev_s = prev_shows[sid]
                    if prev_s.get("avail_status") == "0" and curr_s.get("avail_status") in ["1", "2", "3"]:
                        log(f"ROW/SHOW UNBLOCKED at {vname} ({curr_s['time']} - {s_attr})")
                        msg = format_row_unblock_alert(
                            "Row / Tickets",
                            "#SpiderManBrandNewDay",
                            vname,
                            curr_s["time"],
                            s_attr,
                            date_display,
                            booking_url
                        )
                        send_telegram(msg)
                        
                    prev_cats = {c["price_desc"]: c["avail_status"] for c in prev_s.get("categories", [])}
                    for curr_c in curr_s.get("categories", []):
                        cname = curr_c["price_desc"]
                        if cname in prev_cats and prev_cats[cname] == "0" and curr_c["avail_status"] != "0":
                            log(f"CATEGORY UNBLOCKED: {cname} at {vname} ({curr_s['time']} - {s_attr})")
                            msg = format_row_unblock_alert(
                                f"{cname} / Row",
                                "#SpiderManBrandNewDay",
                                vname,
                                curr_s["time"],
                                s_attr,
                                date_display,
                                booking_url
                            )
                            send_telegram(msg)

    save_current_state(current_state)
    return current_state


def main():
    single_run = "--once" in sys.argv
    log("=========================================")
    log(f"Starting Multi-Format BMS Ticket Tracker ({'Single Check' if single_run else '1-2s Polling Mode'})...")
    log(f"Monitoring URLs count: {len(BMS_URLS)}")
    log("=========================================")
    
    previous_state = load_previous_state()
    
    if single_run:
        run_check_cycle(previous_state)
        return

    consecutive_errors = 0
    while True:
        try:
            previous_state = run_check_cycle(previous_state)
            consecutive_errors = 0
            time.sleep(random.uniform(CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX))
        except Exception as e:
            consecutive_errors += 1
            log(f"Iteration note ({consecutive_errors}): {e}")
            time.sleep(min(CHECK_INTERVAL_MAX * (1.5 ** consecutive_errors), MAX_BACKOFF))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Tracker stopped by user.")
        sys.exit(0)
