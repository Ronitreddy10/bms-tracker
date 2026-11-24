"""Polling loop, change detection and alert formatting."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .bms import (AVAIL, Blocked, BMSError, Client, Show, buytickets_url,
                  pretty_date, seat_layout_url)
from .notify import Notifier
from . import seats as seatmod

log = logging.getLogger(__name__)


# ── config ───────────────────────────────────────────────────────────
@dataclass
class Watch:
    name: str
    movie: str = ""                      # name search, e.g. "avengers doomsday"
    event_codes: list[str] = field(default_factory=list)
    venues: list[str] = field(default_factory=list)    # name keywords or venue codes; empty = all
    formats: list[str] = field(default_factory=list)   # screen keywords, e.g. ["pcx"]; empty = all
    exclude_formats: list[str] = field(default_factory=list)  # skip shows matching these
    seats: bool = False                  # read seat maps to catch row unlocks / seat releases
    rows: str = ""                       # rows you care about, e.g. "H-N" (empty = all)
    seat_classes: list[str] = field(default_factory=list)  # e.g. ["recliner"]
    seat_release_threshold: int = 4      # alert when this many seats free up at once
    city_wide_alerts: bool = False       # also alert when a date opens anywhere in the city
    include_variants: bool = True        # follow other languages/formats (3D, IMAX, Telugu...)
    max_dates: int = 10

    def venue_ok(self, s: Show) -> bool:
        if not self.venues:
            return True
        name = s.venue_name.lower()
        return any(v.lower() in name or v.upper() == s.venue_code for v in self.venues)

    def format_ok(self, s: Show) -> bool:
        if any(f.lower() in s.haystack for f in self.exclude_formats):
            return False
        return not self.formats or any(f.lower() in s.haystack for f in self.formats)

    def matches(self, s: Show) -> bool:
        return self.venue_ok(s) and self.format_ok(s)


@dataclass
class Settings:
    city: str = "hyderabad"
    interval_seconds: int = 180
    jitter_seconds: int = 45
    seat_check_every: int = 2
    listing_check_minutes: int = 20
    request_gap_seconds: float = 2.5
    max_backoff_seconds: int = 1800
    heartbeat_hours: float = 24
    watches: list[Watch] = field(default_factory=list)


def load_settings(path: str | Path) -> Settings:
    import yaml
    raw = yaml.safe_load(Path(path).read_text()) or {}
    watches = [Watch(**w) for w in raw.pop("watches", []) if not w.pop("disabled", False)]
    s = Settings(**raw, watches=watches)
    if not s.watches:
        raise SystemExit("config has no watches")
    for w in s.watches:
        if not (w.movie or w.event_codes):
            raise SystemExit(f"watch '{w.name}' needs `movie` or `event_codes`")
    return s


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ── formatting helpers ───────────────────────────────────────────────
def show_line(s: Show) -> str:
    screen = f" · {s.screen}" if s.screen else ""
    return f"{pretty_date(s.date_code)} {s.time}{screen} @ {s.venue_name}"


def cats_line(s: Show) -> str:
    return "   " + ", ".join(f"{c.name} ₹{c.price} {AVAIL.get(c.status, ('?', '⚪'))[1]}" for c in s.categories)


def rows_filter(rows: list[seatmod.Row], pref: str, classes: list[str]) -> list[seatmod.Row]:
    if classes:
        rows = [r for r in rows if any(c.lower() in r.area.lower() for c in classes)]
    ranges = []
    for part in re.split(r"[,\s]+", (pref or "").upper().strip()):
        if part:
            a, _, b = part.partition("-")
            ranges.append((a, b or a))
    if not ranges:
        return rows
    order = {r.label.upper(): i for i, r in enumerate(rows)}
    out = []
    for r in rows:
        i = order[r.label.upper()]
        for a, b in ranges:
            if a in order and b in order and min(order[a], order[b]) <= i <= max(order[a], order[b]):
                out.append(r)
                break
    return out


# ── the tracker ──────────────────────────────────────────────────────
class Tracker:
    def __init__(self, settings: Settings, state_path: str | Path = "state.json",
                 client: Client | None = None, notifier: Notifier | None = None):
        self.cfg = settings
        self.state_path = Path(state_path)
        self.client = client or Client(settings.city, settings.request_gap_seconds)
        self.notifier = notifier or Notifier()
        self.state = self._load()
        self.cycle = 0
        self._snaps = {}

    # state ------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
        tmp.replace(self.state_path)

    def _wstate(self, w: Watch) -> dict:
        ws = self.state.setdefault("watches", {}).setdefault(w.name, {})
        for k, default in (("events", {}), ("dates", {}), ("venues", []), ("shows", {}), ("seats", {})):
            ws.setdefault(k, default)
        return ws

    # one pass ---------------------------------------------------------
    def check_once(self) -> None:
        self.cycle += 1
        seat_pass = self.cycle % max(1, self.cfg.seat_check_every) == 1 or self.cfg.seat_check_every <= 1
        listings_due = time.time() - self.state.get("last_listing_check", 0) >= self.cfg.listing_check_minutes * 60
        listing_cache: dict[str, dict[str, str]] = {}
        self._snaps = {}  # (event, date) -> Snapshot, shared by watches of the same movie this pass
        for w in self.cfg.watches:
            self._check_watch(w, seat_pass, listings_due, listing_cache)
            self._save()
        if listings_due:
            self.state["last_listing_check"] = time.time()
        self._heartbeat()
        self._save()

    def _showtimes(self, code: str, date: str = ""):
        if (code, date) not in self._snaps:
            self._snaps[(code, date)] = self.client.showtimes(code, date)
        return self._snaps[(code, date)]

    def _check_watch(self, w: Watch, seat_pass: bool, listings_due: bool, cache: dict) -> None:
        ws = self._wstate(w)
        first = not ws.get("initialized")
        alerts: list[str] = []
        urgent = False
        link = ""

        # 1. which ET codes belong to this movie?
        for code in w.event_codes:
            ws["events"].setdefault(code, "")
        if w.movie and (listings_due or first):
            if w.movie not in cache:
                cache[w.movie] = self.client.search_movies(w.movie)
            for code, slug in cache[w.movie].items():
                if code not in ws["events"]:
                    ws["events"][code] = slug
                    if not first:
                        alerts.append(f"🎬 New listing in {self.cfg.city.title()}: {slug.replace('-', ' ').title()} ({code})")
                        urgent = True
                        link = buytickets_url(self.cfg.city, code)

        # 2. fetch showtimes for every open date of every variant
        all_shows: dict[str, Show] = {}
        queue = list(ws["events"])
        seen = set()
        while queue:
            code = queue.pop(0)
            if code in seen:
                continue
            seen.add(code)
            snap = self._showtimes(code)
            if w.include_variants:
                for v in snap.variant_codes:
                    if v not in ws["events"]:
                        ws["events"][v] = ""
                        if not first:
                            alerts.append(f"🎬 New format/language listed: {v}")
                    queue.append(v)
            fetched = {s.date_code for s in snap.shows}
            for s in snap.shows:
                all_shows[s.key] = s
            open_dates = sorted(d for d, st in snap.dates.items() if st == "OPEN")[: w.max_dates]
            for d in open_dates:
                if d in fetched:
                    continue
                for s in self._showtimes(code, d).shows:
                    all_shows[s.key] = s
            # city-wide date openings
            for d, st in snap.dates.items():
                k = f"{code}|{d}"
                prev = ws["dates"].get(k)
                ws["dates"][k] = st
                if w.city_wide_alerts and not first and st == "OPEN" and prev != "OPEN":
                    alerts.append(f"📅 Bookings opened in {self.cfg.city.title()} for {pretty_date(d)}"
                                  f" ({snap.movie})")
                    urgent = True
                    link = link or buytickets_url(self.cfg.city, code, d)

        matched = {k: s for k, s in all_shows.items() if w.matches(s)}

        # 3. diff showtimes
        prev_shows: dict = ws["shows"]
        new_venues, new_shows, new_cats, back = [], [], [], []
        for k, s in sorted(matched.items(), key=lambda kv: (kv[1].date_code, kv[1].time_code)):
            if s.venue_code not in ws["venues"]:
                ws["venues"].append(s.venue_code)
                new_venues.append(s.venue_name)
            old = prev_shows.get(k)
            if old is None:
                new_shows.append(s)
                continue
            old_cats = old.get("cats", {})
            for c in s.categories:
                if c.name not in old_cats:
                    new_cats.append((s, c))
                elif old_cats[c.name] == "0" and c.status != "0":
                    back.append((s, c))
        if not first:
            for v in new_venues:
                alerts.append(f"🏛 {v} just opened bookings!")
                urgent = True
            if new_shows:
                urgent = True
                alerts.append(f"🆕 {len(new_shows)} new show(s):")
                for s in new_shows[:25]:
                    alerts.append("• " + show_line(s))
                    if s.categories:
                        alerts.append(cats_line(s))
                link = link or buytickets_url(self.cfg.city, new_shows[0].event_code, new_shows[0].date_code)
            for s, c in new_cats:
                alerts.append(f"💺 New seat class '{c.name}' (₹{c.price}) opened: {show_line(s)}")
                urgent = True
            for s, c in back:
                alerts.append(f"🔁 {c.name} now bookable ({c.label}): {show_line(s)}")
        today = datetime.now().strftime("%Y%m%d")
        ws["shows"] = {k: v for k, v in prev_shows.items() if v.get("date", "99999999") >= today}
        for k, s in matched.items():
            ws["shows"][k] = {"venue": s.venue_name, "time": s.time, "date": s.date_code, "screen": s.screen,
                              "event": s.event_code, "cats": {c.name: c.status for c in s.categories}}

        # 4. seat maps: rows unlocking / seats released
        if w.seats and seat_pass:
            alerts_s, link_s = self._check_seats(w, ws, matched, first)
            if alerts_s:
                alerts += alerts_s
                urgent = True
                link = link or link_s
        ws["seats"] = {k: v for k, v in ws["seats"].items() if k in ws["shows"]}

        if first:
            ws["initialized"] = True
            venues = sorted({s.venue_name for s in matched.values()})
            self.notifier.send(f"👀 Now watching: {w.name}", [
                f"{len(matched)} matching show(s) right now" + (f" at {', '.join(venues)}" if venues else
                                                                 " (none yet, I'll ping you when they appear)"),
                f"Movie listings found: {', '.join(ws['events']) or 'none yet'}",
            ])
        elif alerts:
            self.notifier.send(f"🎟 {w.name}", alerts, link, urgent)

    def _check_seats(self, w: Watch, ws: dict, matched: dict[str, Show], first: bool):
        alerts, link = [], ""
        for k, s in sorted(matched.items(), key=lambda kv: (kv[1].date_code, kv[1].time_code)):
            try:
                rows = rows_filter(seatmod.fetch_rows(self.client, s.venue_code, s.session_id), w.rows, w.seat_classes)
            except Blocked:
                raise
            except BMSError as e:
                log.info("seat map %s unavailable: %s", k, e)
                continue
            now = {r.label: r.free for r in rows}
            old = ws["seats"].get(k)
            ws["seats"][k] = now
            if old is None or first:
                continue
            unlocked = [f"{r} ({n} free)" for r, n in now.items() if n > 0 and old.get(r, 0) == 0]
            gained = sum(max(0, n - old.get(r, 0)) for r, n in now.items())
            if unlocked:
                alerts.append(f"🔓 Rows opened: {', '.join(unlocked[:12])} — {show_line(s)}")
            elif gained >= w.seat_release_threshold:
                alerts.append(f"🎟 {gained} seats released — {show_line(s)}")
            if (unlocked or gained >= w.seat_release_threshold) and not link:
                link = seat_layout_url(self.cfg.city, s)
        return alerts, link

    def _heartbeat(self):
        if self.cfg.heartbeat_hours <= 0:
            return
        last = self.state.get("last_heartbeat", 0)
        if time.time() - last < self.cfg.heartbeat_hours * 3600:
            return
        if last:
            parts = [f"{w.name}: {len(self._wstate(w)['shows'])} show(s) tracked" for w in self.cfg.watches]
            self.notifier.send("✅ BMS tracker is alive", parts)
        self.state["last_heartbeat"] = time.time()

    # loop -------------------------------------------------------------
    def run_forever(self) -> None:
        failures = 0
        warned = False
        while True:
            try:
                self.check_once()
                if warned:
                    self.notifier.send("✅ BMS tracker recovered", ["BookMyShow is answering again."])
                failures, warned = 0, False
                delay = self.cfg.interval_seconds + random.uniform(0, self.cfg.jitter_seconds)
            except (Blocked, BMSError) as e:
                failures += 1
                self._save()
                delay = min(self.cfg.max_backoff_seconds, self.cfg.interval_seconds * 2 ** failures)
                delay += random.uniform(0, self.cfg.jitter_seconds)
                log.warning("check failed (%s), attempt %d, backing off %.0fs", e, failures, delay)
                if failures >= 3 and not warned:
                    self.notifier.send("⚠️ BMS tracker is struggling", [
                        f"{failures} checks in a row failed: {e}",
                        "If this is HTTP 403, BookMyShow is blocking this machine's IP. "
                        "It must run from an Indian home/mobile connection.",
                    ])
                    warned = True
            except Exception:
                log.exception("unexpected error")
                failures += 1
                delay = min(self.cfg.max_backoff_seconds, self.cfg.interval_seconds * 2 ** failures)
            time.sleep(delay)
