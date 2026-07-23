#!/usr/bin/env python3
"""
BookMyShow Multi-Date & City-Wide High-Speed Ticket Tracker
------------------------------------------------------------
Monitors ALL DATES (July 30, July 31, Aug 1, Aug 2, etc.) and ALL 45+ THEATRES IN HYDERABAD
for new show releases, new dates opening, and seat/row unblocks.

USAGE:
    Daemon Mode (1-2s Interval):
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
from playwright.sync_api import sync_playwright

# ============ CONFIGURATION ============
BASE_BUY_URL = os.getenv(
    "BMS_URL",
    "https://in.bookmyshow.com/movies/hyderabad/spider-man-brand-new-day/buytickets/ET00505091"
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8794059592:AAFdRlSpuRiZYZItgD71DPeO-y6DgNeEaDY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1209851846")

# Polling Interval
CHECK_INTERVAL_MIN = 1.0
CHECK_INTERVAL_MAX = 2.0
MAX_BACKOFF = 30

# File Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "bms_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "tracker.log")

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


def format_date_display(date_code: str) -> str:
    try:
        dt_obj = datetime.strptime(date_code, "%Y%m%d")
        day = dt_obj.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"{day}{suffix} {dt_obj.strftime('%B')}"
    except Exception:
        return date_code


def parse_event_code(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    for part in path_parts:
        if re.match(r"^ET\d{8,}$", part):
            return part
    return "ET00505091"


def extract_all_dates_and_venues(playwright):
    """
    Extracts all available date codes dynamically from BookMyShow,
    then fetches showtimes for all venues across ALL dates!
    """
    event_code = parse_event_code(BASE_BUY_URL)
    base_url = f"https://in.bookmyshow.com/movies/hyderabad/spider-man-brand-new-day/buytickets/{event_code}/"
    
    all_data = {}
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        # Initial page load to discover active date codes
        first_url = base_url + "20260730"
        page.goto(first_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        
        date_codes = page.evaluate("""() => {
            const state = window.__INITIAL_STATE__;
            if (!state) return [];
            const results = [];
            function searchObj(obj) {
                if (obj && typeof obj === 'object') {
                    if (obj.dateCode || obj.showDateCode) {
                        const d = obj.dateCode || obj.showDateCode;
                        if (/^[0-9]{8}$/.test(d)) results.push(d);
                    }
                    for (let k in obj) searchObj(obj[k]);
                }
            }
            searchObj(state);
            return [...new Set(results)].sort();
        }""")
        
        if not date_codes:
            date_codes = ["20260730", "20260731", "20260801", "20260802"]
            
        log(f"Active dates detected on BookMyShow: {date_codes}")
        
        for dcode in date_codes:
            d_url = base_url + dcode
            page.goto(d_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            
            st_func = page.evaluate(
                "() => window.__INITIAL_STATE__ && window.__INITIAL_STATE__.showtimesFunctionalApi ? window.__INITIAL_STATE__.showtimesFunctionalApi.queries : {}"
            )
            
            for qk, qv in st_func.items():
                if "fetchPrimaryDynamic" in qk and isinstance(qv, dict) and "data" in qv:
                    data = qv["data"].get("data", {})
                    widgets = data.get("showtimeWidgets", [])
                    for w in widgets:
                        if w.get("type") == "groupList":
                            for group in w.get("data", []):
                                for vc in group.get("data", []):
                                    add_data = vc.get("additionalData", {})
                                    vname = add_data.get("venueName", "Unknown Venue")
                                    vcode = add_data.get("venueCode", "")
                                    
                                    raw_showtimes = vc.get("showtimes", [])
                                    if not raw_showtimes:
                                        for sec in vc.get("showtimesSections", []):
                                            raw_showtimes.extend(sec.get("showtimes", []))
                                    
                                    showtimes = []
                                    for st in raw_showtimes:
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
                                            "categories": categories
                                        })
                                        
                                    key = f"{vname}_{dcode}"
                                    all_data[key] = {
                                        "venue_name": vname,
                                        "venue_code": vcode,
                                        "date_code": dcode,
                                        "showtimes": showtimes,
                                        "target_url": d_url
                                    }
        browser.close()
    except Exception as e:
        log(f"Multi-date citywide scan error: {e}")
        
    return all_data


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
        f"🔗 <a href=\"{booking_url}\">Book Tickets on BookMyShow</a>"
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


def run_check_cycle(playwright, previous_state):
    current_state = extract_all_dates_and_venues(playwright)
    
    if not current_state:
        log("No venues/dates returned during scan iteration.")
        return previous_state
        
    if not previous_state:
        log(f"Initial baseline saved for {len(current_state)} venue-date combinations.")
        save_current_state(current_state)
        return current_state
        
    for key, vdata in current_state.items():
        vname = vdata["venue_name"]
        dcode = vdata["date_code"]
        date_display = format_date_display(dcode)
        booking_url = vdata.get("target_url", BASE_BUY_URL)
        
        if key not in previous_state:
            log(f"NEW SHOWS / DATE OPENED: {vname} on {date_display}")
            msg = format_new_showtime_alert(vname, vdata.get("showtimes", []), date_display, booking_url)
            send_telegram(msg)
        else:
            prev_shows = {s["session_id"]: s for s in previous_state[key].get("showtimes", [])}
            curr_shows = vdata.get("showtimes", [])
            
            new_shows = [s for s in curr_shows if s["session_id"] not in prev_shows]
            if new_shows:
                log(f"NEW SHOWTIMES at {vname} on {date_display}: {[s['time'] for s in new_shows]}")
                msg = format_new_showtime_alert(vname, new_shows, date_display, booking_url)
                send_telegram(msg)
                
            for curr_s in curr_shows:
                sid = curr_s["session_id"]
                s_attr = curr_s.get("screen_attr", "")
                
                if sid in prev_shows:
                    prev_s = prev_shows[sid]
                    if prev_s.get("avail_status") == "0" and curr_s.get("avail_status") in ["1", "2", "3"]:
                        log(f"ROW/SHOW UNBLOCKED at {vname} ({date_display}, {curr_s['time']} - {s_attr})")
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
                            log(f"CATEGORY UNBLOCKED: {cname} at {vname} ({date_display}, {curr_s['time']} - {s_attr})")
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
    log(f"Starting Multi-Date & City-Wide BMS Ticket Tracker ({'Single Check' if single_run else 'Continuous Scanning'})...")
    log("=========================================")
    
    previous_state = load_previous_state()
    
    with sync_playwright() as playwright:
        if single_run:
            run_check_cycle(playwright, previous_state)
            return

        consecutive_errors = 0
        while True:
            try:
                previous_state = run_check_cycle(playwright, previous_state)
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
