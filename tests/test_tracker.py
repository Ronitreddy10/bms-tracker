"""Offline tests: fake BookMyShow responses, check the alerts we'd send."""
import base64
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bms_tracker import seats as seatmod  # noqa: E402
from bms_tracker.bms import match_movie_links, parse_showtimes  # noqa: E402
from bms_tracker.tracker import Settings, Tracker, Watch, rows_filter  # noqa: E402

ET = "ET00999001"


def show(session, time_code, attr, cats, date="20261218"):
    return {"title": time_code, "screenAttr": attr, "additionalData": {
        "sessionId": session, "showDateCode": date, "showTime": time_code, "showTimeCode": time_code,
        "categories": [{"priceDesc": n, "curPrice": p, "availStatus": st} for n, p, st in cats]}}


def venue(code, name, shows):
    return {"type": "venue-card", "additionalData": {"venueCode": code, "venueName": name}, "showtimes": shows}


def payload(venues, dates=None):
    dates = dates or {"20261218": "date-selected", "20261219": "date-disabled"}
    return {"data": {
        "topStickyWidgets": [{"type": "horizontal-block-list", "data": [
            {"id": d, "styleId": st, "data": [1, 2, 3]} for d, st in dates.items()]}],
        "showtimeWidgets": [{"type": "groupList", "data": [{"type": "venueGroup", "data": venues}]}],
    }}


class FakeClient:
    def __init__(self):
        self.data = payload([])
        self.rows = {}
        self.calls = 0

    def search_movies(self, q):
        return {ET: "avengers-doomsday"}

    def showtimes(self, code, date=""):
        self.calls += 1
        return parse_showtimes(self.data, code)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, lines, link="", urgent=False):
        self.sent.append((title, lines, link, urgent))


def make(tmp_path, watches, seats_fn=None):
    s = Settings(city="hyderabad", seat_check_every=1, heartbeat_hours=0, watches=watches)
    c, n = FakeClient(), FakeNotifier()
    t = Tracker(s, tmp_path / "state.json", client=c, notifier=n)
    return t, c, n


def test_movie_link_matching():
    html = '<a href="/movies/hyderabad/avengers-doomsday/ET00999001">x</a><a href="/movies/hyderabad/other/ET00111111">'
    assert match_movie_links(html, "hyderabad", ["doomsday"]) == {ET: "avengers-doomsday"}
    assert match_movie_links(html, "hyderabad", ["avengers", "doomsday"]) == {ET: "avengers-doomsday"}


def test_new_pcx_show_alerts_only_pcx_watch(tmp_path):
    w_pcx = Watch(name="pcx", movie="doomsday", venues=["prasad"], formats=["pcx"])
    w_rest = Watch(name="rest", movie="doomsday", venues=["prasad"], exclude_formats=["pcx"])
    t, c, n = make(tmp_path, [w_pcx, w_rest])
    c.data = payload([venue("PRHY", "Prasads Multiplex: Hyderabad",
                            [show("1", "1000", "2D", [("GOLD", "295", "3")])])])
    t.check_once()
    assert [x[0] for x in n.sent] == ["👀 Now watching: pcx", "👀 Now watching: rest"]
    n.sent.clear()

    # Prasads opens the PCX screen
    c.data = payload([venue("PRHY", "Prasads Multiplex: Hyderabad", [
        show("1", "1000", "2D", [("GOLD", "295", "3")]),
        show("2", "1900", "PCX", [("PCX RECLINER", "450", "3")]),
    ])])
    calls_before = c.calls
    t.check_once()
    assert c.calls - calls_before == 1, "two watches on one movie should share one fetch"
    assert len(n.sent) == 1
    title, lines, link, urgent = n.sent[0]
    assert title == "🎟 pcx" and urgent
    assert any("PCX RECLINER" in l for l in lines)
    assert not any("GOLD" in l for l in lines)
    assert "buytickets/ET00999001/20261218" in link

    # nothing changed -> silence
    n.sent.clear()
    t.check_once()
    assert n.sent == []


def test_new_venue_and_class_and_back_in_stock(tmp_path):
    w = Watch(name="allu", movie="doomsday", venues=["allu"], formats=["dolby"])
    t, c, n = make(tmp_path, [w])
    t.check_once()
    n.sent.clear()
    c.data = payload([venue("ALLU", "Allu Cinemas: Kokapet", [show("9", "2100", "DOLBY CINEMA", [("PRIME", "500", "0")])])])
    t.check_once()
    lines = n.sent[-1][1]
    assert any("Allu Cinemas" in l and "opened bookings" in l for l in lines)
    n.sent.clear()
    c.data = payload([venue("ALLU", "Allu Cinemas: Kokapet", [
        show("9", "2100", "DOLBY CINEMA", [("PRIME", "500", "2"), ("RECLINER", "700", "3")])])])
    t.check_once()
    lines = n.sent[-1][1]
    assert any("New seat class 'RECLINER'" in l for l in lines)
    assert any("PRIME now bookable" in l for l in lines)


def test_city_wide_date_open(tmp_path):
    w = Watch(name="w", movie="doomsday", venues=["prasad"], city_wide_alerts=True)
    t, c, n = make(tmp_path, [w])
    t.check_once()
    n.sent.clear()
    c.data = payload([], {"20261218": "date-selected", "20261219": "date-default"})
    t.check_once()
    assert any("Bookings opened" in l and "19 Dec" in l for l in n.sent[-1][1])


def test_row_unlock(tmp_path, monkeypatch):
    w = Watch(name="pcx", movie="doomsday", formats=["pcx"], seats=True)
    t, c, n = make(tmp_path, [w])
    c.data = payload([venue("PRHY", "Prasads", [show("2", "1900", "PCX", [("PCX", "450", "3")])])])
    state = {"rows": [seatmod.Row("A", "PCX", 0, 20), seatmod.Row("B", "PCX", 2, 20)]}
    monkeypatch.setattr(seatmod, "fetch_rows", lambda cl, v, s: state["rows"])
    t.check_once()
    n.sent.clear()
    state["rows"] = [seatmod.Row("A", "PCX", 14, 20), seatmod.Row("B", "PCX", 2, 20)]
    t.check_once()
    title, lines, link, urgent = n.sent[-1]
    assert any("Rows opened: A (14 free)" in l for l in lines)
    assert "seat-layout" in link


def test_parse_layout_and_rows_filter():
    # live format: rowNo:letter:areaMarker:seats...; "A0+0" = blank; spacer rows have no letter
    text = ("RECLINER:A:RC:1||1:A:A000:A0+0:A1001+1:A1002+2:A0+0:A2003+3|0::A000:A0+0"
            "|2:B:A000:A1004+4:A2005+5|3:C:A000:A2006+6")
    rows = seatmod.parse_layout(text)
    assert [(r.label, r.free, r.total, r.area) for r in rows] == [
        ("A", 2, 3, "RECLINER"), ("B", 1, 2, "RECLINER"), ("C", 0, 1, "RECLINER")]
    assert [r.label for r in rows_filter(rows, "B-C", [])] == ["B", "C"]
    assert [r.label for r in rows_filter(rows, "", ["recl"])] == ["A", "B", "C"]


def test_decrypt_roundtrip():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    msg = b"A:1:1101+1||"
    pad = 16 - len(msg) % 16
    e = Cipher(algorithms.AES(seatmod.SEAT_KEY), modes.CBC(bytes(16))).encryptor()
    ct = e.update(msg + bytes([pad]) * pad) + e.finalize()
    assert seatmod._decrypt(base64.b64encode(ct).decode()) == msg.decode()


def test_no_shows_yet_payload():
    snap = parse_showtimes({"metadata": {"error": "There are no shows"}, "data": {}}, ET)
    assert snap.shows == [] and snap.dates == {}


def test_screen_text_not_duplicated():
    st = show("1", "1900", "LASER 4K ATMOS", [("GOLD", "200", "3")])
    st["additionalData"]["attributes"] = "LASER 4K ATMOS"
    snap = parse_showtimes(payload([venue("ALUC", "ALLU Cinemas: Kokapet", [st])]), ET)
    assert snap.shows[0].screen == "LASER 4K ATMOS"


def test_explore_link_form():
    html = '<a href="/hyderabad/movies/avengers-doomsday/ET00439706">'
    assert match_movie_links(html, "hyderabad", ["doomsday"]) == {"ET00439706": "avengers-doomsday"}
