"""Record each runway's real designators in airbases.json, for the runway numbers MapBuilder paints.

airbases.py keeps a runway's two ends ("start" = OurAirports' low end, "end" = its high end) but not their
names. This step matches every runway in airbases.json (the main one and each extra one) to its OurAirports row by
its ends and writes "leIdent" / "heIdent" (e.g. "05L" / "23R") beside it. The painter keeps the digits only (the
game's marking atlas has no letters); a runway OurAirports names without digits gets its number from its heading.

  python runway_idents.py <map> [<map> ...]
"""
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from airbases import runway_ends  # noqa: E402
from mapgeo import MapGeo  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OA = ROOT / "data" / "ourairports"


def match(ends, rows):
    """The OurAirports (le, he) idents of the row whose ends are nearest, swapped if the row runs the other way."""
    (x1, z1, x2, z2) = ends
    best = None
    for geom, r in rows:
        a1, b1, a2, b2 = geom
        same = math.hypot(a1 - x1, b1 - z1) + math.hypot(a2 - x2, b2 - z2)
        flip = math.hypot(a1 - x2, b1 - z2) + math.hypot(a2 - x1, b2 - z1)
        d, le, he = (same, r["le_ident"], r["he_ident"]) if same <= flip else (flip, r["he_ident"], r["le_ident"])
        if best is None or d < best[0]:
            best = (d, le, he)
    if best is None or best[0] > 400:
        return "", ""
    return best[1] or "", best[2] or ""


def main(mp):
    geo = MapGeo.load(ROOT / "maps" / mp / "map.json")
    path = ROOT / "build" / mp / "airbases.json"
    bases = json.loads(path.read_text(encoding="utf-8"))
    airports = {r["ident"]: r for r in csv.DictReader(open(OA / "airports.csv", encoding="utf-8"))}
    by_airport = {}
    for r in csv.DictReader(open(OA / "runways.csv", encoding="utf-8")):
        by_airport.setdefault(r["airport_ident"], []).append(r)
    out = []
    for b in bases:
        ap = airports.get(b.get("ident", ""))
        rows = []
        for r in by_airport.get(b.get("ident", ""), []):
            if r["closed"] == "1" or not r["length_ft"]:
                continue
            try:
                geom, _, _ = runway_ends(geo, r, ap)
            except (ValueError, KeyError):
                continue
            rows.append((geom, r))
        s, c = math.sin(math.radians(b["heading"])), math.cos(math.radians(b["heading"]))
        h = b["runwayLength"] / 2
        main_ends = (b["x"] - s * h, b["z"] - c * h, b["x"] + s * h, b["z"] + c * h)
        b["leIdent"], b["heIdent"] = match(main_ends, rows)
        names = [f"{b['leIdent'] or '?'}/{b['heIdent'] or '?'}"]
        for e in b.get("extraRunways") or []:
            e["leIdent"], e["heIdent"] = match((e["startX"], e["startZ"], e["endX"], e["endZ"]), rows)
            names.append(f"{e['leIdent'] or '?'}/{e['heIdent'] or '?'}")
        out.append(f"{b['uniqueName']} {' '.join(names)}")
    path.write_text(json.dumps(bases, indent=2), encoding="utf-8")
    print(f"{mp}: " + "; ".join(out))


if __name__ == "__main__":
    for a in sys.argv[1:]:
        main(a)
