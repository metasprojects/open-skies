"""Decide, once per airbase, which side of the runway its apron is on, and record it in airbases.json as
"apronSide" (+1 = right of the runway's heading, -1 = left).

Both the Unity stage (MapBuilder: taxiway, apron and floor paving) and the missions (MissionBuilder.furnish_airbase:
hangars, shelters, helipads and tower) read it, so the hangars always stand on the paved apron. NOCustomMaps'
own notes: an aircraft in a hangar on grass sinks and breaks its gear rolling out. The side is the one whose
apron band is flattest and driest on the map's heights, the same test furnish_airbase used to make on its own.

  python apron_side.py <map>
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mapgeo import MapGeo  # noqa: E402
from placement import MapGround  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def side_for(g, base):
    inner = base.get("runwayWidth", 45.0) / 2 + 140.0
    along_half = base["flatHalfAlong"]
    s, c = math.sin(math.radians(base["heading"])), math.cos(math.radians(base["heading"]))
    best = None
    for side in (1, -1):
        ax, az = c * side, -s * side
        pts = [(base["x"] + ax * (inner + d) + s * t, base["z"] + az * (inner + d) + c * t)
               for d in (40, 120, 200) for t in (-0.5 * along_half, -0.25 * along_half, 0.0, 0.25 * along_half, 0.5 * along_half)]
        ok = sum(abs(g.height(x, z) - base["y"]) < 3.0 and g.height(x, z) > 0.5 and not g.on_runway(x, z, margin=10)
                 for x, z in pts)
        if best is None or ok > best[0]:
            best = (ok, side)
    return best[1]


def main(mp):
    geo = MapGeo.load(ROOT / "maps" / mp / "map.json")
    g = MapGround(ROOT / "build" / mp, geo.cfg)
    path = ROOT / "build" / mp / "airbases.json"
    bases = json.loads(path.read_text(encoding="utf-8"))
    for b in bases:
        b["apronSide"] = side_for(g, b)
    path.write_text(json.dumps(bases, indent=2), encoding="utf-8")
    print(f"{mp}: " + ", ".join(f"{b['uniqueName']} {'+' if b['apronSide'] > 0 else '-'}" for b in bases))


if __name__ == "__main__":
    for a in sys.argv[1:]:
        main(a)
