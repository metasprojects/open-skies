"""A Free Flight mission for any Open Skies map: both sides, every aircraft unlocked, nobody shooting.

Each side flies from the airbases it was given, each furnished with hangars, shelters, helipads and a tower (a
custom airfield is bare otherwise, and aircraft spawn only from hangars). Where the map has open sea, each side also
gets a carrier and a destroyer in real deep water near its own bases. Every unit goes through the pipeline's placement checker and the
mission through its strict lint, so the mission only gets written if it will load.

Usage: freeflight.py <map.json> <build dir> <prefab cm.<id>.<hash8>> <missions out dir> <title>
Prints "NOTE: ..." lines for the build page.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from mapgeo import MapGeo  # noqa: E402
from missions import ALL_AIRCRAFT, MissionBuilder  # noqa: E402
from placement import MapGround  # noqa: E402

SIDES = ("Boscali", "Primeva")
CARRIER_DEPTH = 25.0       # metres of water: a fleet carrier needs 16, with room to turn
CARRIER_SHORE_M = 2500.0
EDGE_CLEAR_M = 14000.0     # beyond the 9 km edge slope, where the sea is real


def compass(dx, dz):
    names = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
    return names[int(((math.degrees(math.atan2(dx, dz)) + 360 + 22.5) % 360) // 45)]


def sea_spots(g):
    """Coarse grid (about 400 m) of points deep enough for a carrier group, far enough from shore and edge."""
    step = 8
    h = np.asarray(g.h[::step, ::step], dtype=np.float32)
    pitch = g.pitch * step
    deep = h < -CARRIER_DEPTH
    shore = ndimage.distance_transform_edt(h < 0) * pitch
    n = h.shape[0]
    idx = np.arange(n) * pitch - g.half
    xs = idx[None, :].repeat(n, 0)
    zs = (g.half - np.arange(n) * pitch)[:, None].repeat(n, 1)
    ok = deep & (shore > CARRIER_SHORE_M) & (np.abs(xs) < g.half - EDGE_CLEAR_M) & (np.abs(zs) < g.half - EDGE_CLEAR_M)
    return xs[ok], zs[ok]


def main(cfg_path, build, prefab, out, title):
    geo = MapGeo.load(cfg_path)
    cfg = geo.cfg
    g = MapGround(build, cfg)
    bases = {s: [a for a in g.airbases if a["faction"] == s] for s in SIDES}
    name = cfg["displayName"]

    sx, sz = sea_spots(g)
    carriers = {}
    if len(sx):
        used = []
        for side in SIDES:
            own = bases[side]
            if own:
                cx = float(np.mean([a["x"] for a in own]))
                cz = float(np.mean([a["z"] for a in own]))
            else:     # no airbases: put its fleet on the opposite side of the map from the other side's bases
                other = bases[SIDES[1 - SIDES.index(side)]]
                cx = -float(np.mean([a["x"] for a in other])) if other else 0.0
                cz = -float(np.mean([a["z"] for a in other])) if other else 0.0
            d = np.hypot(sx - cx, sz - cz)
            for u in used:     # keep the two fleets at least 30 km apart
                d = np.where(np.hypot(sx - u[0], sz - u[1]) < 30000, np.inf, d)
            if np.isfinite(d.min()):
                i = int(np.argmin(d))
                carriers[side] = (float(sx[i]), float(sz[i]))
                used.append(carriers[side])

    places = ", ".join(a["displayName"] for a in g.airbases)
    desc = (f"Fly {name}: real terrain, coastlines and sea depth, with {len(g.airbases)} real airbase"
            f"{'s' if len(g.airbases) != 1 else ''} ({places}). Every aircraft is unlocked and nobody shoots at you: "
            "explore the map, practise landings, plan your own missions. Made with Open Skies. "
            f"Requires NOCustomMaps and the {name} map.")
    if len(desc) > 900:
        desc = desc[:897] + "..."

    def build_mission(with_ships):
        mb = MissionBuilder(g, prefab, desc, time=8.5, weather=0.15, rank=6)
        for f in SIDES:
            mb.faction(f, balance=999999.0, income=0.0, join=9999.0, warheads=999, ai_aircraft=0,
                       supplies={t: 99 for t in ALL_AIRCRAFT})
        placed = {}
        if with_ships:
            for side, (x, z) in carriers.items():
                yaw = math.degrees(math.atan2(-x, -z)) % 360        # bow towards the middle of the map
                tag = "BDF" if side == "Boscali" else "PALA"
                c = mb.ship("FleetCarrier1", side, x, z, yaw=yaw, name=f"{tag}_Carrier")
                s, co = math.sin(math.radians(yaw)), math.cos(math.radians(yaw))
                mb.ship("Destroyer1", side, x - 1500 * s + 1200 * co, z - 1500 * co - 1200 * s, yaw=yaw,
                        name=f"{tag}_Escort")
                if c:
                    placed[side] = (x, z)
        # Since NOCustomMaps 1.3 a custom airfield is bare until the mission furnishes it, and aircraft spawn only
        # from hangars: every airbase gets hangars, shelters, helipads and a tower (the pipeline's furnish_airbase).
        flown = {s: [] for s in SIDES}
        for base in g.airbases:
            before = len(mb.problems)
            units = mb.furnish_airbase(base, base["faction"])
            trouble = mb.problems[before:]
            del mb.problems[before:]
            if units and not trouble:
                flown[base["faction"]].append(base)
            else:
                bare.add(base["displayName"])
        objs = []
        for side in SIDES:
            own = flown[side]
            parts = []
            if own:
                parts.append(f"{side} airbases: " + ", ".join(a["displayName"] for a in own) + ".")
            if side in placed:
                x, z = placed[side]
                ref = min(own, key=lambda a: math.hypot(a["x"] - x, a["z"] - z)) if own else None
                if ref:
                    km = math.hypot(x - ref["x"], z - ref["z"]) / 1000
                    parts.append(f"Your carrier is {km:.0f} km {compass(x - ref['x'], z - ref['z'])} of "
                                 f"{ref['displayName']}.")
                else:
                    parts.append(f"Your carrier is in the {compass(x, z)} of the map.")
            if not parts:
                continue
            msg = mb.message(f"Welcome_{side[0]}", f"{name} free flight. " + " ".join(parts))
            objs.append(mb.objective("None", f"Free Flight {side}", side, display="Free flight", outcomes=[msg]))
        go = mb.start("Start_FreeFlight", objs)
        mb.m["objectives"].insert(0, {"Type": "None", "UniqueName": "Mission Start", "Faction": "", "DisplayName": "",
                                      "Hidden": True, "Outcomes": [go]})
        # A side with nowhere to spawn cannot be joined.
        for f in mb.m["factions"]:
            side = f["factionName"]
            if not flown[side] and side not in placed:
                f["preventJoin"] = True
        return mb, placed, flown

    bare = set()
    mb, placed, flown = build_mission(bool(carriers))
    problems = mb.check()
    if problems and carriers:
        print("ship placement problems, writing the mission without ships:", problems)
        bare.clear()
        mb, placed, flown = build_mission(False)
        problems = mb.check()
        print("NOTE: The carriers could not be placed safely, so the Free Flight mission has none.")
    for b in sorted(bare):
        print(f"NOTE: {b}: no flat room beside the runway for hangars, so aircraft cannot start there.")
    if problems:
        print("mission problems:\n  " + "\n  ".join(problems))
        return 1
    if not carriers:
        print("NOTE: No open sea deep enough for a carrier group, so the Free Flight mission has no ships.")
    for side in SIDES:
        if not flown[side] and side not in placed:
            print(f"NOTE: {side} has no usable airbase and no carrier, so players can only join the other side.")
    hangars = sum(1 for u in mb.m["buildings"] if u["type"] in ("hangar_med", "shelter1"))
    print(f"airbases furnished: {sum(len(v) for v in flown.values())} of {len(g.airbases)}, {hangars} hangars and shelters")
    d = mb.write(out, title)
    print("written to", d)
    print(json.dumps({"ships": len(mb.m["ships"]), "carriers": sorted(placed), "buildings": len(mb.m["buildings"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
