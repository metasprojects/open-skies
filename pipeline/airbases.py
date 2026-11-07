"""Place the map's airbases on their real runways and level the ground under them.

Reads the map config's "airbases" list (OurAirports idents, faction, names), the OurAirports
CSVs and build/heights.npy. Writes:
    heights.npy (overwritten with the levelled terrain; the unlevelled one is kept as heights_raw.npy)
    airbases.bytes   NOCustomMaps airbase data (NOAIRB03/05, see Core/AirbaseData.cs, MIT)
    airbases.json    the same, readable, for the Unity builder and for review
"""
import csv
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

from mapgeo import MapGeo

FT = 0.3048
APRON_ACROSS = 700.0      # levelled ground either side of the runway centreline, metres
APRON_ALONG = 450.0       # levelled ground past each runway end
BLEND = 500.0             # distance over which the levelled pad blends back into the terrain
MIN_PAD_Y = 4.0


def write_str(buf, s):
    data = s.encode("utf-8")
    n = len(data)
    while True:
        b = n & 0x7F
        n >>= 7
        buf += bytes([b | (0x80 if n else 0)])
        if not n:
            break
    buf += data


def runway_ends(geo, r, airport):
    """Game-space ends (x1, z1, x2, z2), length and width of an OurAirports runway row."""
    width = float(r["width_ft"] or 150) * FT
    if r["le_latitude_deg"] and r["he_latitude_deg"]:
        x1, z1 = geo.game_xz(float(r["le_latitude_deg"]), float(r["le_longitude_deg"]))
        x2, z2 = geo.game_xz(float(r["he_latitude_deg"]), float(r["he_longitude_deg"]))
    else:
        x0, z0 = geo.game_xz(float(airport["latitude_deg"]), float(airport["longitude_deg"]))
        hdg = math.radians(float(r["le_heading_degT"] or (int((r["le_ident"] or "09")[:2]) * 10)))
        half = float(r["length_ft"]) * FT / 2 / geo.scale
        dx, dz = math.sin(hdg) * half, math.cos(hdg) * half
        x1, z1, x2, z2 = x0 - dx, z0 - dz, x0 + dx, z0 + dz
    length = math.hypot(x2 - x1, z2 - z1)
    return (x1, z1, x2, z2), length, width / geo.scale


def pad_mask(geo, h, cx, cz, hdg, half_along, half_across):
    """Distance outside the levelled rectangle for every sample (0 inside), and the sample grid."""
    n = h.shape[0]
    pitch = geo.cfg["vertexPitchMetres"]
    half = (n - 1) * pitch / 2
    reach = math.hypot(half_along, half_across) + BLEND
    c0 = max(0, int((cx - reach + half) / pitch))
    c1 = min(n, int((cx + reach + half) / pitch) + 2)
    r0 = max(0, int((half - (cz + reach)) / pitch))
    r1 = min(n, int((half - (cz - reach)) / pitch) + 2)
    xs = -half + np.arange(c0, c1) * pitch
    zs = half - np.arange(r0, r1) * pitch
    X, Z = np.meshgrid(xs, zs)
    s, c = math.sin(math.radians(hdg)), math.cos(math.radians(hdg))
    along = (X - cx) * s + (Z - cz) * c
    across = (X - cx) * c - (Z - cz) * s
    out_along = np.maximum(np.abs(along) - half_along, 0)
    out_across = np.maximum(np.abs(across) - half_across, 0)
    return np.hypot(out_along, out_across), (slice(r0, r1), slice(c0, c1))


def main(cfg_path, oa_dir, build_dir):
    geo = MapGeo.load(cfg_path)
    build = Path(build_dir)
    raw_path = build / "heights_raw.npy"
    if not raw_path.exists():
        (build / "heights.npy").replace(raw_path)
    h = np.load(raw_path)
    oa = Path(oa_dir)
    airports = {r["ident"]: r for r in csv.DictReader(open(oa / "airports.csv", encoding="utf-8"))}
    runways = {}
    for r in csv.DictReader(open(oa / "runways.csv", encoding="utf-8")):
        if r["airport_ident"] in airports and r["closed"] != "1" and r["length_ft"]:
            runways.setdefault(r["airport_ident"], []).append(r)

    placed = []
    for spec in geo.cfg["airbases"]:
        a = airports[spec["ident"]]
        rws = sorted(runways[spec["ident"]], key=lambda r: -int(r["length_ft"]))
        long_rws = [r for r in rws if int(r["length_ft"]) >= 4000]
        if not long_rws and rws:
            # A short field (e.g. Stanley, 3,013 ft) keeps its longest strip: fine for helicopters and light
            # aircraft, too short for most jets. The loader has no minimum runway length.
            print(f"  {spec['ident']}: no runway of 4,000 ft or more, keeping its longest ({rws[0]['length_ft']} ft)")
            long_rws = rws[:1]
        if not long_rws:
            raise SystemExit(f"{spec['ident']}: OurAirports lists no usable runway")
        rws = long_rws[: 1 + 8]
        geoms = [runway_ends(geo, r, a) for r in rws]
        (x1, z1, x2, z2), length, width = geoms[0]
        cx, cz = (x1 + x2) / 2, (z1 + z2) / 2
        hdg = math.degrees(math.atan2(x2 - x1, z2 - z1)) % 360
        s, c = math.sin(math.radians(hdg)), math.cos(math.radians(hdg))
        half_along, half_across = length / 2 + APRON_ALONG, APRON_ACROSS
        for (ex1, ez1, ex2, ez2), elen, _ in geoms[1:]:
            for ex, ez in ((ex1, ez1), (ex2, ez2)):
                half_along = max(half_along, abs((ex - cx) * s + (ez - cz) * c) + APRON_ALONG)
                half_across = max(half_across, abs((ex - cx) * c - (ez - cz) * s) + APRON_ACROSS)
        dist, win = pad_mask(geo, h, cx, cz, hdg, half_along, half_across)
        inside = dist == 0
        y = max(float(np.median(h[win][inside])), MIN_PAD_Y)
        t = np.clip(dist / BLEND, 0, 1)
        t = t * t * (3 - 2 * t)
        h[win] = y + (h[win] - y) * t
        placed.append({
            "uniqueName": spec["name"], "displayName": spec["short"], "faction": spec["faction"],
            "ident": spec["ident"], "x": cx, "y": y, "z": cz, "heading": hdg,
            "runwayLength": length, "runwayWidth": width,
            "flatHalfAlong": half_along, "flatHalfAcross": half_across,
            "extraRunways": [{"startX": g[0][0], "startZ": g[0][1], "endX": g[0][2], "endZ": g[0][3], "width": g[2]}
                             for g in geoms[1:]],
        })
        print(f"  {spec['name']:28s} {spec['faction']:8s} y={y:6.1f} hdg={hdg:5.1f} len={length:5.0f} "
              f"pad {2*half_along:5.0f} x {2*half_across:5.0f} m, {len(geoms)-1} extra runways")

    np.save(build / "heights.npy", h.astype(np.float32))
    version = 5 if any(p["extraRunways"] for p in placed) else 3
    buf = bytearray(b"NOAIRB0" + str(version).encode())
    buf += struct.pack("<i", len(placed))
    for p in placed:
        for k in ("uniqueName", "displayName", "faction"):
            write_str(buf, p[k])
        buf += struct.pack("<8f", p["x"], p["y"], p["z"], p["heading"], p["runwayLength"], p["runwayWidth"],
                           p["flatHalfAlong"], p["flatHalfAcross"])
        if version >= 4:
            buf += struct.pack("<i", 0)
        if version >= 5:
            buf += struct.pack("<i", len(p["extraRunways"]))
            for e in p["extraRunways"]:
                buf += struct.pack("<5f", e["startX"], e["startZ"], e["endX"], e["endZ"], e["width"])
    (build / "airbases.bytes").write_bytes(bytes(buf))
    (build / "airbases.json").write_text(json.dumps(placed, indent=2), encoding="utf-8")
    print(f"{len(placed)} airbases, NOAIRB0{version}, {len(buf)} bytes")


if __name__ == "__main__":
    main(*sys.argv[1:])
