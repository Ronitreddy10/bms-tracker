"""BookMyShow web client (read-only).

Showtimes parsing is adapted from aviiciii/bms-ticket-notifier (MIT) and
karanloyola/bms-telegram-tracker (MIT). See NOTICE.md.

This only *reads* the same data the BookMyShow website shows a visitor.
It never books, holds or locks seats.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from curl_cffi import requests

BASE = "https://in.bookmyshow.com"
SHOWTIMES_API = f"{BASE}/api/movies-data/v4/showtimes-by-event/primary-dynamic"

AVAIL = {
    "0": ("Sold out", "🔴"),
    "1": ("Almost full", "🟡"),
    "2": ("Filling fast", "🟠"),
    "3": ("Available", "🟢"),
}

DATE_STYLE = {
    "date-selected": "OPEN",
    "date-default": "OPEN",
    "date-disabled": "NOT_OPEN",
}

# slug -> (regionCode, canonical slug, lat, lon, geohash)
REGIONS = {
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
    "vizag": ("VIZA", "vizag-visakhapatnam", "17.686", "83.218", "tg4"),
    "vijayawada": ("VIJA", "vijayawada", "16.506", "80.648", "tfv"),
}


class BMSError(Exception):
    """Something went wrong talking to BookMyShow."""


class Blocked(BMSError):
    """HTTP 403/429: we're being rate limited or blocked. Back off."""


@dataclass
class Category:
    name: str
    price: str
    status: str  # "0".."3"

    @property
    def label(self) -> str:
        lbl, ico = AVAIL.get(self.status, ("Unknown", "⚪"))
        return f"{ico} {lbl}"


@dataclass
class Show:
    event_code: str
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen: str = ""          # screen / format attributes, e.g. "PCX", "DOLBY CINEMA"
    categories: list[Category] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.venue_code}|{self.session_id}"

    @property
    def haystack(self) -> str:
        """Everything we know about this show's screen, for keyword matching."""
        cats = " ".join(c.name for c in self.categories)
        return f"{self.venue_name} {self.screen} {cats}".lower()


@dataclass
class Snapshot:
    event_code: str
    movie: str
    dates: dict[str, str]          # date_code -> OPEN / NOT_OPEN
    shows: list[Show]
    variant_codes: set[str]        # other ET codes (languages/formats) of the same movie


def region(city: str):
    key = city.lower().strip()
    if key in REGIONS:
        return REGIONS[key]
    return (key.upper()[:6], key, "0", "0", "")


class Client:
    """A polite HTTP client: Chrome TLS fingerprint, spaced-out requests."""

    def __init__(self, city: str, min_gap: float = 2.0):
        self.city = city
        self.min_gap = min_gap
        self._last = 0.0
        self.session = requests.Session(impersonate="chrome")

    def _pace(self):
        wait = self.min_gap + random.uniform(0, self.min_gap) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _get(self, url, **kw):
        self._pace()
        try:
            r = self.session.get(url, timeout=25, **kw)
        except Exception as e:
            raise BMSError(f"network error: {e}") from e
        if r.status_code in (403, 429):
            raise Blocked(f"HTTP {r.status_code} from BookMyShow")
        if r.status_code != 200:
            raise BMSError(f"HTTP {r.status_code} from BookMyShow")
        return r

    # ── movie discovery ──────────────────────────────────────────────
    def search_movies(self, query: str) -> dict[str, str]:
        """Find a movie's ET codes from the city's now-showing + upcoming pages.
        Returns {event_code: url_slug}."""
        _, slug, *_ = region(self.city)
        words = re.sub(r"[^a-z0-9]+", " ", query.lower()).split()
        found: dict[str, str] = {}
        for page in (f"explore/movies-{slug}", f"explore/upcoming-movies-{slug}"):
            html = self._get(f"{BASE}/{page}").text
            found.update(match_movie_links(html, slug, words))
        return found

    # ── showtimes ────────────────────────────────────────────────────
    def showtimes(self, event_code: str, date_code: str = "") -> Snapshot:
        code, slug, lat, lon, geohash = region(self.city)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE}/movies/{slug}/buytickets/{event_code}/",
            "x-app-code": "WEB",
            "x-region-code": code,
            "x-region-slug": slug,
            "x-geohash": geohash,
            "x-latitude": lat,
            "x-longitude": lon,
            "x-location-selection": "manual",
        }
        params = {
            "eventCode": event_code, "dateCode": date_code, "isDesktop": "true",
            "regionCode": code, "xLocationShared": "false",
            "memberId": "", "lsId": "", "subCode": "", "lat": lat, "lon": lon,
        }
        r = self._get(SHOWTIMES_API, headers=headers, params=params)
        try:
            data = r.json()
        except ValueError as e:
            raise BMSError("BookMyShow returned non-JSON (layout change or block page)") from e
        return parse_showtimes(data, event_code)


# ── parsers (pure functions, unit-tested) ────────────────────────────
def match_movie_links(html: str, city_slug: str, words: list[str]) -> dict[str, str]:
    out = {}
    pat = r"/(?:%s/movies|movies/%s)/([a-z0-9-]+)/(ET\d{8,})" % (re.escape(city_slug), re.escape(city_slug))
    for slug, code in re.findall(pat, html):
        name_words = slug.split("-")
        if words and all(any(nw.startswith(w) for nw in name_words) for w in words):
            out.setdefault(code, slug)
    return out


def parse_showtimes(data: dict, event_code: str) -> Snapshot:
    body = data.get("data") or {}
    err = (data.get("metadata") or {}).get("error")
    if err and not body.get("showtimeWidgets"):
        # e.g. "There are no shows available in this region." -> not open yet
        return Snapshot(event_code, event_code, {}, [], set())
    fs = body.get("bottomSheetData", {}).get("format-selector", {})
    variants = set(re.findall(r"ET\d{8,}", json.dumps(fs))) - {event_code}
    return Snapshot(event_code, _movie_name(body, event_code), _dates(body),
                    _shows(body, event_code), variants)


def _movie_name(body, fallback):
    for w in body.get("bottomSheetData", {}).get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle" and d.get("text"):
                    return d["text"]
    return fallback


def _dates(body):
    out = {}
    for w in body.get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            if item.get("id") and re.fullmatch(r"\d{8}", str(item["id"])):
                out[str(item["id"])] = DATE_STYLE.get(item.get("styleId", ""), "UNKNOWN")
    return out


def _shows(body, event_code):
    shows = []
    for w in body.get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                va = card.get("additionalData", {})
                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(sa.get("showDateCode") or sa.get("dateCode") or "")
                    if not date_code and re.match(r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]
                    parts = []
                    for x in (st.get("screenAttr", ""), sa.get("attributes", ""), sa.get("screenName", "")):
                        x = (x or "").strip() if isinstance(x, str) else ""
                        if x and x not in parts:
                            parts.append(x)  # screenAttr and attributes usually repeat each other
                    screen = " ".join(parts)
                    show = Show(
                        event_code=event_code,
                        venue_code=va.get("venueCode", ""),
                        venue_name=va.get("venueName", "Unknown"),
                        session_id=str(sa.get("sessionId", "")),
                        date_code=date_code,
                        time=sa.get("showTime") or st.get("title", ""),
                        time_code=str(sa.get("showTimeCode", "")),
                        screen=screen,
                    )
                    for c in sa.get("categories", []):
                        show.categories.append(Category(
                            name=c.get("priceDesc", ""),
                            price=str(c.get("curPrice", "0")),
                            status=str(c.get("availStatus", "")),
                        ))
                    shows.append(show)
    return shows


# ── links ────────────────────────────────────────────────────────────
def buytickets_url(city: str, event_code: str, date_code: str = "") -> str:
    _, slug, *_ = region(city)
    return f"{BASE}/movies/{slug}/buytickets/{event_code}/{date_code}".rstrip("/")


def seat_layout_url(city: str, show: Show) -> str:
    _, slug, *_ = region(city)
    return (f"{BASE}/movies/{slug}/seat-layout/{show.event_code}/"
            f"{show.venue_code}/{show.session_id}/{show.date_code}")


def pretty_date(date_code: str) -> str:
    from datetime import datetime
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b")
    except ValueError:
        return date_code


def parse_movie_url(url: str) -> str | None:
    for p in urlparse(url.strip()).path.split("/"):
        if re.fullmatch(r"ET\d{8,}", p):
            return p
    return None
