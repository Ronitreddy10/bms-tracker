"""CLI.

  python -m bms_tracker run            # 24/7 loop (default)
  python -m bms_tracker once           # single check, then exit
  python -m bms_tracker discover "avengers doomsday"   # list ET codes, theatres, screen names
  python -m bms_tracker test-notify    # send a test alert
"""
from __future__ import annotations

import argparse
import logging
from collections import defaultdict

from .bms import BMSError, Client, pretty_date
from .notify import Notifier
from .tracker import Tracker, load_dotenv, load_settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="bms_tracker")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "once", "discover", "test-notify"])
    ap.add_argument("query", nargs="?", default="", help="movie name or ET code (discover)")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-s", "--state", default="state.json")
    ap.add_argument("--city", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    load_dotenv()

    if a.command == "test-notify":
        Notifier().send("🎟 BMS tracker test", ["If you can read this, notifications work."],
                        "https://in.bookmyshow.com", urgent=True)
        return

    if a.command == "discover":
        city = a.city or "hyderabad"
        try:
            discover(Client(city), city, a.query)
        except BMSError as e:
            raise SystemExit(f"BookMyShow request failed: {e}\n(403 = this network/IP is blocked; run from an Indian home connection)")
        return

    settings = load_settings(a.config)
    if a.city:
        settings.city = a.city
    t = Tracker(settings, a.state)
    if a.command == "once":
        try:
            t.check_once()
        except BMSError as e:
            raise SystemExit(f"BookMyShow request failed: {e}")
    else:
        logging.info("Watching %d target(s) in %s every ~%ss", len(settings.watches), settings.city,
                     settings.interval_seconds)
        t.run_forever()


def discover(client: Client, city: str, query: str) -> None:
    """Print everything you need to fill in config.yaml."""
    codes = [query] if query.startswith("ET") else list(client.search_movies(query))
    if not codes:
        print(f"No listing for '{query}' in {city} yet. Put `movie: {query}` in config and the tracker "
              "will alert you the moment it appears.")
        return
    todo, seen = list(codes), set()
    while todo:
        code = todo.pop(0)
        if code in seen:
            continue
        seen.add(code)
        snap = client.showtimes(code)
        todo += sorted(snap.variant_codes)
        dates = ", ".join(f"{pretty_date(d)}{'' if s == 'OPEN' else ' (closed)'}" for d, s in sorted(snap.dates.items()))
        print(f"\n=== {snap.movie}  [{code}]\n    dates: {dates or 'not open yet'}")
        by_venue = defaultdict(set)
        for s in snap.shows:
            by_venue[(s.venue_code, s.venue_name)].add(
                f"{s.screen or '-'} | " + ", ".join(c.name for c in s.categories))
        for (vc, vn), screens in sorted(by_venue.items(), key=lambda kv: kv[0][1]):
            print(f"    {vc:6} {vn}")
            for sc in sorted(screens):
                print(f"           screen/classes: {sc}")


if __name__ == "__main__":
    main()
