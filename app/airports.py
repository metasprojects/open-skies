"""Real airports inside a map area, from OurAirports (public domain), with the checks the pipeline needs.

Only airports the pipeline can really place are offered: an open runway with a length, a heading or both
end positions, not water. Each one says in plain words what it is good for (short strips: helicopters and
light aircraft only) and why it is greyed out when it can't be used (too close to the map edge).
"""
from __future__ import annotations

import csv
import json
import math
import re
import unicodedata

from . import downloads, paths

TILE_M = 5120.0
EDGE_FADE_M = 9000.0       # the pipeline slopes the outer 9 km into the sea
JET_RUNWAY_FT = 4000       # the pipeline's own threshold for a "long" runway (airbases.py)
GOOD_RUNWAY_FT = 6000
MAX_AIRBASES = 16
PAD_ALONG_M = 450 + 500    # apron past each runway end + blend back into the terrain (airbases.py)
PAD_ACROSS_M = 700 + 500

_INDEX = None
MILITARY = re.compile(r"\b(air ?base|airbase|AB|AFB|air force|naval air|NAS|RAF|military|army air|airfield)\b", re.I)


def _usable_runway(r) -> bool:
    if r["closed"] == "1" or not r["length_ft"].strip().isdigit():
        return False
    if "WAT" in (r["surface"] or "").upper():
        return False
    if r["le_latitude_deg"] and r["he_latitude_deg"]:
        return True
    hdg = (r["le_heading_degT"] or "").strip()
    try:
        float(hdg)
        return True
    except ValueError:
        pass
    ident = (r["le_ident"] or "09")[:2]
    return ident.isdigit()


def index() -> list[dict]:
    """Compact list of every airport with at least one runway the pipeline can use (built once, kept on disk)."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    d = downloads.ourairports()
    cache = d / "openskies_index.json"
    src = d / "airports.csv"
    if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
        _INDEX = json.loads(cache.read_text(encoding="utf-8"))
        return _INDEX
    # The runways airbases.py will use, chosen exactly as it chooses them: every open runway with a length,
    # longest first; those of 4,000 ft or more (else just the longest); at most 9. All of them must be placeable.
    per = {}
    with open(d / "runways.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["closed"] != "1" and r["length_ft"].strip():
                per.setdefault(r["airport_ident"], []).append(r)
    best = {}
    for ident, rws in per.items():
        if not all(r["length_ft"].strip().isdigit() for r in rws):
            continue
        rws.sort(key=lambda r: -int(r["length_ft"]))
        use = [r for r in rws if int(r["length_ft"]) >= JET_RUNWAY_FT] or rws[:1]
        use = use[:9]
        if all(_usable_runway(r) for r in use):
            best[ident] = {"ft": int(use[0]["length_ft"]), "n": len(use), "surface": (use[0]["surface"] or "").strip()}
    out = []
    with open(src, encoding="utf-8") as f:
        for a in csv.DictReader(f):
            if a["type"] not in ("large_airport", "medium_airport", "small_airport"):
                continue
            b = best.get(a["ident"])
            if not b:
                continue
            try:
                lat, lon = float(a["latitude_deg"]), float(a["longitude_deg"])
            except ValueError:
                continue
            out.append([a["ident"], a["name"], a["type"], round(lat, 5), round(lon, 5), b["ft"], b["n"],
                        b["surface"], a["municipality"], a["iso_country"]])
    cache.write_text(json.dumps(out), encoding="utf-8")
    _INDEX = out
    return out


def ascii_name(s: str, limit: int = 40) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("(*)", "").replace("(Duplicate)", "")          # OurAirports' own markers
    # The Unity stage names files after airbases and the map (Assets/Map/Meshes/<name>_runway.asset): keep only
    # characters every file system and the game's lobby accept.
    s = re.sub(r"[^A-Za-z0-9 .,'()&-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .,-")
    return s[:limit].rstrip(" .,-")


def short_name(name: str, ident: str) -> str:
    s = ascii_name(name)
    for w in (" International Airport", " International", " Regional Airport", " Airport", " Air Base", " Airbase",
              " Airfield", " Aerodrome", " Air Force Base", " Naval Air Station"):
        s = s.replace(w, "")
    s = s.strip(" -/,") or ident
    if len(s) > 20:                       # the game's short label: cut at a word, not mid-word
        cut = s[:21].rsplit(" ", 1)[0] if " " in s[:21] else s[:20]
        s = cut if len(cut) >= 4 else s[:20]
    return s.rstrip(" .,-&")


def frame(lat0: float, lon0: float):
    from pyproj import CRS, Transformer
    crs = CRS.from_proj4(f"+proj=tmerc +lat_0={lat0} +lon_0={lon0} +k=1 +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs")
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True), Transformer.from_crs(crs, "EPSG:4326", always_xy=True)


def area_bounds(lat0, lon0, tiles, margin_m=0.0):
    """(south, west, north, east) of the square map area plus a margin, and the square's corners."""
    _, to_geo = frame(lat0, lon0)
    half = tiles * TILE_M / 2 + margin_m
    lons, lats = [], []
    corners = []
    for x in (-half, 0, half):
        for y in (-half, 0, half):
            lo, la = to_geo.transform(x, y)
            lons.append(lo)
            lats.append(la)
            if x and y:
                corners.append([la, lo])
    return (min(lats), min(lons), max(lats), max(lons)), corners


def in_area(lat0, lon0, tiles) -> list[dict]:
    to_local, _ = frame(lat0, lon0)
    half = tiles * TILE_M / 2
    (s, w, n, e), _ = area_bounds(lat0, lon0, tiles, 2000)
    rows = [a for a in index() if s <= a[3] <= n and w <= a[4] <= e]
    out = []
    for ident, name, typ, lat, lon, ft, nrw, surface, town, country in rows:
        x, z = to_local.transform(lon, lat)
        if max(abs(x), abs(z)) > half:
            continue
        # The levelled pad reaches half the runway plus 950 m from the airport's reference point.
        reach = ft * 0.3048 / 2 + PAD_ALONG_M
        clear = half - max(abs(x), abs(z)) - EDGE_FADE_M
        a = {"ident": ident, "name": ascii_name(name) or ident, "short": short_name(name, ident), "type": typ,
             "lat": lat, "lon": lon, "x": round(x), "z": round(z), "runwayFt": ft, "runways": nrw,
             "surface": surface, "place": ascii_name(f"{town}, {country}" if town else country, 60),
             "military": bool(MILITARY.search(name)), "ok": True, "note": ""}
        if clear < reach:
            a.update(ok=False, note="Too close to the map edge (the outer 9 km slopes into the sea). Move the box.")
        elif ft < JET_RUNWAY_FT:
            a["note"] = f"Short runway ({ft:,} ft): helicopters and light aircraft only."
        elif ft < GOOD_RUNWAY_FT:
            a["note"] = f"{ft:,} ft runway: fine for most aircraft, tight for heavy jets."
        out.append(a)
    out.sort(key=lambda a: (not a["ok"], {"large_airport": 0, "medium_airport": 1}.get(a["type"], 2), -a["runwayFt"]))
    choose_defaults(out)
    return out


def overlap(a: dict, b: dict) -> bool:
    """Two airports whose levelled pads would run into each other (the pipeline levels each one separately)."""
    ra = a["runwayFt"] * 0.3048 / 2 + PAD_ALONG_M
    rb = b["runwayFt"] * 0.3048 / 2 + PAD_ALONG_M
    return math.hypot(a["x"] - b["x"], a["z"] - b["z"]) < ra + rb - 400


def choose_defaults(rows: list[dict]) -> None:
    """Tick the big fields and the air bases (up to 8), never two that overlap; sides split east and west."""
    picked = []
    for a in rows:
        good = a["ok"] and (a["type"] == "large_airport" or
                            (a["runwayFt"] >= GOOD_RUNWAY_FT and (a["type"] == "medium_airport" or a["military"])))
        a["selected"] = False
        if good and len(picked) < 8 and not any(overlap(a, p) for p in picked):
            a["selected"] = True
            picked.append(a)
    if not picked:
        for a in rows:
            if a["ok"] and a["runwayFt"] >= JET_RUNWAY_FT:
                a["selected"] = True
                break
    for a in rows:
        a["side"] = "Boscali" if a["x"] >= 0 else "Primeva"


def check_selection(rows: list[dict]) -> list[str]:
    """Problems that stop a build, in plain words."""
    sel = [a for a in rows if a.get("selected")]
    probs = []
    if not sel:
        probs.append("Tick at least one airport: players need somewhere to take off.")
    if len(sel) > MAX_AIRBASES:
        probs.append(f"Tick at most {MAX_AIRBASES} airports.")
    for a in sel:
        if not a["ok"]:
            probs.append(f"{a['name']}: {a['note']}")
    for i, a in enumerate(sel):
        for b in sel[i + 1:]:
            if overlap(a, b):
                probs.append(f"{a['name']} and {b['name']} are so close their runways overlap. Untick one.")
    names = [a["short"].lower() for a in sel]
    for a in sel:
        if names.count(a["short"].lower()) > 1:
            a["short"] = f"{a['short'][:14]} {a['ident']}"[:20]
    return probs
