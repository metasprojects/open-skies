"""<Map> Free Flight (JsonVersion 6), for any Open Skies map: both sides, every aircraft, a carrier group each,
nobody shooting. Reads the sides and airbases from the map's own build (airbases.json).

Carriers: map.json "freeFlight": {"Boscali": [lat, lon, heading], "Primeva": [...]} when given; otherwise each
side's carrier goes in open water at least 40 m deep and 6 km offshore, as close as possible to that side's
airbases (a side with no airbase gets the open water farthest from the other side's bases).

Usage: make_freeflight.py <map.json> <build dir> <map prefab, e.g. cm.taiwanstrait.39eea304> <out missions folder>
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mapgeo import MapGeo  # noqa: E402
from missions import ALL_AIRCRAFT, MissionBuilder  # noqa: E402
from placement import MapGround  # noqa: E402

SIDES = ("Boscali", "Primeva")


def open_water(g, x, z, radius=90000.0, depth=None, offshore=6000.0, edge=15000.0):
    """Deep open water if there is any near (x, z), else shallower (the Yellow Sea off Korea is mostly under
    40 m): 40 m, then 25 m, then 16 m (a fleet carrier draws about 11 m; sea_problem keeps 4 m under it)."""
    for d in ([depth] if depth else [40.0, 25.0, 16.0]):
        p = _open_water(g, x, z, radius, d, offshore, edge)
        if p:
            return p
    return None


def _open_water(g, x, z, radius, depth, offshore, edge):
    """Nearest point to (x, z) where a fleet carrier can sit in deep, open water, clear of the map's outer
    band (sloped into the sea on purpose, so its depth is not real)."""
    def bad(a, b):
        if not g.inside(a, b, edge) or g.sea_problem(a, b, "FleetCarrier1"):
            return "no"
        if -g.height(a, b) < depth or g.shore_distance(a, b) < offshore:
            return "no"
        return None
    return g.find(x, z, bad, radius, step=1500.0)


def main(cfg, build, prefab, out):
    geo = MapGeo.load(cfg)
    c = geo.cfg
    g = MapGround(build, c)
    bases = json.loads(Path(build, "airbases.json").read_text(encoding="utf-8"))
    by_side = {s: [b for b in bases if b.get("faction") == s] for s in SIDES}
    name = c["displayName"]
    title = f"{name} Free Flight"
    listing = "; ".join(f"{s}: " + (", ".join(b.get('displayName') or b['uniqueName'] for b in by_side[s]) or "carrier only")
                        for s in SIDES)
    description = (f"Fly the real {name}: Copernicus terrain, real coastlines, roads and towns, "
                   f"{len(bases)} real airbases ({listing}). Every aircraft is unlocked and nobody shoots at you: "
                   f"explore the map, practise carrier landings, plan your own missions. "
                   f"Requires NOCustomMaps and the {name} map (Open Skies).")
    mb = MissionBuilder(g, prefab, description, time=8.5, weather=0.15, rank=6)
    for f in SIDES:
        mb.faction(f, balance=999999.0, income=0.0, join=9999.0, warheads=999, ai_aircraft=0,
                   supplies={t: 99 for t in ALL_AIRCRAFT})

    fixed = c.get("freeFlight", {})
    centre = {s: ((sum(b["x"] for b in by_side[s]) / len(by_side[s]), sum(b["z"] for b in by_side[s]) / len(by_side[s]))
                  if by_side[s] else None) for s in SIDES}
    spots = {}
    for s in SIDES:
        if s in fixed:
            lat, lon, hdg = fixed[s]
            x, z = geo.game_xz(lat, lon)
            spots[s] = (x, z, hdg)
            continue
        other = centre[SIDES[1 - SIDES.index(s)]]
        edge = c.get("edgeFadeMetres", 9000) + 6000.0
        if centre[s]:
            p = open_water(g, *centre[s], edge=edge)
        else:
            # No airfield: the far side of the map from the other side's bases.
            ox, oz = other or (0.0, 0.0)
            r = geo.size_m / 2 - 25000
            far = max(((r * math.sin(a), r * math.cos(a)) for a in [k * math.pi / 8 for k in range(16)]),
                      key=lambda q: math.hypot(q[0] - ox, q[1] - oz))
            p = open_water(g, *far, edge=edge)
        if not p:
            raise SystemExit(f"no open water for the {s} carrier group")
        # Face the other side's bases (or the map centre).
        tx, tz = other or (0.0, 0.0)
        spots[s] = (p[0], p[1], math.degrees(math.atan2(tx - p[0], tz - p[1])) % 360)

    tag = {"Boscali": "BDF", "Primeva": "PALA"}
    for s in SIDES:
        x, z, hdg = spots[s]
        mb.ship("FleetCarrier1", s, x, z, yaw=hdg, name=f"{tag[s]}_Carrier")
        ex, ez = x - 1500 * math.cos(math.radians(hdg)), z + 1500 * math.sin(math.radians(hdg))
        e = g.find_sea(ex, ez, "Destroyer1") or (x + 1800, z)
        mb.ship("Destroyer1", s, e[0], e[1], yaw=hdg, name=f"{tag[s]}_Escort")

    mb.furnish_airbases(bases)
    objs = []
    for s in SIDES:
        names = ", ".join(b.get("displayName") or b["uniqueName"] for b in by_side[s])
        text = (f"{name} free flight. {s} bases: {names}. Your carrier is out at sea." if names
                else f"{name} free flight. {s} has no airfield here: fly from your carrier.")
        objs.append(mb.objective("None", f"Free Flight {s}", s, display="Free flight",
                                 outcomes=[mb.message(f"Welcome_{tag[s]}", text)]))
    go = mb.start("Start_FreeFlight", objs)
    mb.m["objectives"].insert(0, {"Type": "None", "UniqueName": "Mission Start", "Faction": "", "DisplayName": "",
                                  "Hidden": True, "Outcomes": [go]})
    problems = mb.check()
    print("placement problems:", problems or "none")
    for sh in mb.m["ships"]:
        p = sh["globalPosition"]
        print(f"  {sh['UniqueName']}: x={p['x']:.0f} z={p['z']:.0f} depth {-g.height(p['x'], p['z']):.0f} m, "
              f"{g.shore_distance(p['x'], p['z']) / 1000:.1f} km offshore")
    print("written to", mb.write(out, title))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
