"""Seat-map reader, used to spot rows being unlocked / seats being released.

Adapted from karanloyola/bms-telegram-tracker (MIT). See NOTICE.md.

The BookMyShow seat page loads its map from doTrans.aspx (GETSEATLAYOUT).
The browser decodes the response with a key shipped in BMS's own web bundle;
we decode it the same way so we can count free seats per row. Read-only:
this never selects, holds or books a seat.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from .bms import BMSError, Blocked, Client

SEAT_URL = "https://services-in.bookmyshow.com/doTrans.aspx"
SEAT_KEY = b"kYp3s6v9y$B&E)H+MbQeThWmZq4t7w!z"
FREE = {"1", "4"}  # 1 = available, 4 = available ("bestseller")


@dataclass
class Row:
    label: str
    area: str          # seat class name, e.g. "RECLINER"
    free: int
    total: int


def _decrypt(b64: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    ct = base64.b64decode(b64)
    d = Cipher(algorithms.AES(SEAT_KEY), modes.CBC(bytes(16))).decryptor()
    pt = d.update(ct) + d.finalize()
    return pt[:-pt[-1]].decode()


def fetch_rows(client: Client, venue_code: str, session_id: str) -> list[Row]:
    from curl_cffi import CurlMime
    fields = dict(strAppCode="WEB", lngTransactionIdentifier="0", strCommand="GETSEATLAYOUT",
                  strVenueCode=venue_code, strParam1=session_id, strParam2="WEB", strParam3="",
                  strParam4="", strParam5="Y", strParam6="", strParam7="", strParam8="",
                  strParam9="", strParam10="", strFormat="json")
    mp = CurlMime()
    for k, v in fields.items():
        mp.addpart(name=k, data=v.encode())
    client._pace()
    try:
        r = client.session.post(SEAT_URL, multipart=mp, timeout=25,
                                headers={"Origin": "https://in.bookmyshow.com",
                                         "Referer": "https://in.bookmyshow.com/"})
    except Exception as e:
        raise BMSError(f"seat map network error: {e}") from e
    if r.status_code in (403, 429):
        raise Blocked(f"seat map HTTP {r.status_code}")
    if r.status_code != 200:
        raise BMSError(f"seat map HTTP {r.status_code}")
    j = (r.json() or {}).get("BookMyShow", {})
    if j.get("blnSuccess") != "true" or not j.get("strData"):
        raise BMSError(f"seat map unavailable: {j.get('strException') or 'no data'}")
    return parse_layout(_decrypt(j["strData"]))


def parse_layout(text: str) -> list[Row]:
    """Format: "<areas>||<rows>".
    areas: "desc:areaId:areaCode:...|..."   rows: "label:rowNo:SEAT:SEAT...|..."
    SEAT:  "<areaId><status><seatNo>+<cinemaSeatNo>[+rank]"
    """
    parts = text.split("||")
    if len(parts) < 2:
        return []
    area_desc = {}
    for a in filter(None, parts[0].split("|")):
        f = a.split(":")
        if len(f) >= 2:
            area_desc[f[1]] = f[0]
    rows = []
    for r in filter(None, parts[1].split("|")):
        f = r.split(":")
        if len(f) < 2 or not f[0]:
            continue
        free = total = 0
        areas = {}
        for tok in f[2:]:
            head = tok.split("+")[0]
            if len(head) < 2:
                continue  # gangway
            total += 1
            areas[head[0]] = areas.get(head[0], 0) + 1
            if head[1] in FREE:
                free += 1
        area_id = max(areas, key=areas.get) if areas else ""
        rows.append(Row(label=f[0], area=area_desc.get(area_id, area_id), free=free, total=total))
    return rows
