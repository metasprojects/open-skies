"""More missions for any Open Skies map, made with the pipeline's scenario templates (pipeline\\scenarios.py):

  Escalation   both sides, PvP or co-op against AI. Every airbase can be captured; each side has a factory complex
               at its main base and SAM rings and a garrison at every base. A side wins by taking every enemy
               airbase and destroying the enemy factories (sinking the enemy carriers where a side has no airbase).
  Strike       one side (the players) hits the radar, fuel and ammunition beside the other side's main airbase
               within 40 minutes, through SAM rings and AI fighters. One Strike per side that has a target.

Each mission is tried on its own. One that can't be made on this map (nowhere for a side to start) is left out
with a "NOTE: ..." line for the build page; the build carries on. An airbase with no flat room for hangars is left
bare (the templates' "bareBasesOk"): it can still be captured or struck, but nobody spawns there, so a side whose
bases are all bare counts as having none (it needs a carrier). Every unit goes through the
pipeline's placement checker and every mission through its strict lint before it is written.

Usage: scenario_missions.py <map.json> <build dir> <prefab cm.<id>.<hash8>> <missions out dir> <map name>
"""
import contextlib
import io
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import numpy as np  # noqa: E402

import scenarios  # noqa: E402
from freeflight import sea_spots  # noqa: E402  (app\freeflight.py, next to this file)
from mapgeo import MapGeo  # noqa: E402
from missions import MissionBuilder  # noqa: E402
from placement import MapGround  # noqa: E402

SIDES = scenarios.SIDES
SUFFIX = re.compile(r"\s+(international\s+)?(airport|airfield|aerodrome|air\s*base|air\s+force\s+base|airbase|"
                    r"air\s+station|naval\s+air\s+station|ab|afb)$", re.I)


def safe_title(t: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", t).strip(" .")[:60].strip(" .")


def short(base: dict) -> str:
    n = base.get("displayName") or base["uniqueName"]
    s = SUFFIX.sub("", n).strip()
    return s or n


def clip(text: str, n: int = 900) -> str:
    return text if len(text) <= n else text[:n - 3] + "..."


def km_dir(a, b):
    dx, dz = b["x"] - a["x"], b["z"] - a["z"]
    names = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
    return math.hypot(dx, dz) / 1000, names[int(((math.degrees(math.atan2(dx, dz)) + 360 + 22.5) % 360) // 45)]


class Map:
    def __init__(self, cfg_path, build, prefab):
        self.cfg_path, self.build, self.prefab = str(cfg_path), str(build), prefab
        self.geo = MapGeo.load(cfg_path)
        self.g = MapGround(build, self.geo.cfg)
        self.bases = json.loads(Path(build, "airbases.json").read_text(encoding="utf-8"))
        self.side = {s: sorted((a for a in self.bases if a.get("faction") == s),
                               key=lambda a: -float(a.get("runwayLength") or 0)) for s in SIDES}
        self._sea = None
        self.bare = self.bare_bases()
        # where each side's aircraft can start: airbases with room for hangars
        self.spawn = {s: [a for a in self.side[s] if a["uniqueName"] not in self.bare] for s in SIDES}

    def latlon(self, x, z):
        lon, lat = self.geo.to_geo().transform(x * self.geo.scale, z * self.geo.scale)
        return [round(lat, 5), round(lon, 5)]

    def sea(self):
        if self._sea is None:
            self._sea = sea_spots(self.g)
        return self._sea

    def carrier_spot(self, side):
        """Deep open water for a side with no airbase: away from the other side's bases, as in Free Flight."""
        sx, sz = self.sea()
        if not len(sx):
            return None
        other = self.side[scenarios.other(side)]
        cx = -float(np.mean([a["x"] for a in other])) if other else 0.0
        cz = -float(np.mean([a["z"] for a in other])) if other else 0.0
        i = int(np.argmin(np.hypot(sx - cx, sz - cz)))
        return self.latlon(float(sx[i]), float(sz[i]))

    def bare_bases(self):
        """uniqueNames of the airbases with no room for hangars (the templates leave them bare)."""
        mb = MissionBuilder(self.g, self.prefab, "check", time=8.0, weather=0.2, rank=3)
        bare = []
        for a in self.bases:
            before = len(mb.problems)
            units = mb.furnish_airbase(a, a["faction"])
            if not units or len(mb.problems) > before:
                bare.append(a["uniqueName"])
            del mb.problems[before:]
        return set(bare)


def escalation_spec(m: Map, name: str):
    carriers, starts = {}, []
    for s in SIDES:
        if m.side[s] and not m.spawn[s]:
            return None, f"{s}'s airbases have no room for hangars, so its aircraft have nowhere to start"
        if m.side[s]:
            starts.append(f"{s}: " + ", ".join(short(a) for a in m.spawn[s]))
        else:
            spot = m.carrier_spot(s)
            if not spot:
                return None, f"{s} has no airbase and the map has no open sea for its carrier"
            carriers[s] = spot
            starts.append(f"{s}: a carrier group at sea")
    if not any(m.side[s] for s in SIDES):
        return None, "neither side has an airbase"
    desc = (f"{name.upper()}: ESCALATION. PvP, or co-op against the AI. Every airbase can be captured, income grows "
            f"with every base you hold, and each side has a factory complex at its main base and SAMs and a garrison "
            f"at every base. Take all the enemy's airbases and destroy their factories"
            f"{' (or sink their carriers)' if carriers else ''} to win. {'; '.join(starts)}. "
            f"Made with Open Skies. Requires NOCustomMaps and the {name} map. Fictional scenario, fictional factions.")
    spec = {"title": safe_title(f"{name} - Escalation"), "description": clip(desc), "time": 7.0, "weather": 0.2,
            "ai": 4, "bareBasesOk": True, "factories": {s: m.spawn[s][0]["uniqueName"] for s in SIDES if m.spawn[s]}}
    if carriers:
        spec["carriers"] = carriers
    for s in SIDES:
        e = scenarios.other(s)
        what = "airbases and factories" if m.side[e] else "carriers"
        spec.setdefault("brief", {})[s] = f"Take every {e} {what}. Income grows with every base you hold."
    return spec, None


def strike_spec(m: Map, name: str, att: str):
    dfn = scenarios.other(att)
    if not m.side[dfn]:
        return None, f"{dfn} has no airbase to strike"
    main = m.side[dfn][0]
    tgt = short(main)
    spec = {"title": safe_title(f"{name} - Strike on {tgt}"), "attacker": att, "time": 6.5, "weather": 0.25,
            "bareBasesOk": True,
            "minutes": 40, "percent": 0.8, "ai": {att: 2, dfn: 4},
            "display": f"Destroy the radar, fuel and ammunition at {tgt}",
            "targets": [
                {"name": "Radar", "type": "radarStation1", "base": main["uniqueName"], "extra": 700},
                {"name": "Fuel", "type": "fuelTank1", "base": main["uniqueName"], "extra": 1100, "count": 2,
                 "spacing": 90},
                {"name": "Ammo", "type": "ammoDump", "base": main["uniqueName"], "extra": 1500, "count": 2,
                 "spacing": 140}],
            "baseRings": [a["uniqueName"] for a in m.side[dfn][:3]],
            "winText": f"{tgt} is out of action. Head home."}
    if m.spawn[att]:
        home = min(m.spawn[att], key=lambda a: math.hypot(a["x"] - main["x"], a["z"] - main["z"]))
        km, d = km_dir(home, main)
        start = f"From {short(home)}, {km:.0f} km {d}"
    else:
        spot = m.carrier_spot(att)
        if not spot:
            return None, f"{att} has no airbase with room for hangars and the map has no open sea for its carrier"
        spec["carrier"] = spot
        start = "From a carrier at sea"
    others = [short(a) for a in m.side[dfn][1:3]]
    hangars = [short(a) for a in m.spawn[dfn]]
    fighters = f" Fighters fly from {', '.join(hangars)}." if hangars else ""
    spec["description"] = clip(
        f"{name.upper()}: STRIKE ON {tgt.upper()}. {start}, hit {tgt}'s radar, fuel and ammunition within 40 minutes, "
        f"through the SAMs around {dfn}'s airbases.{fighters} Solo or co-op as {att}; {dfn} is flown by the AI. "
        f"Made with Open Skies. Requires NOCustomMaps and the {name} map. Fictional scenario, fictional factions.")
    spec["intel"] = [f"{tgt}'s radar, fuel and ammunition are the targets.",
                     f"SAMs cover {tgt}" + (f" and {', '.join(others)}." if others else ".") +
                     (" Expect fighters." if hangars else ""),
                     "Destroy at least four of the five targets, then head home."]
    if spec.get("carrier"):
        spec["intel"][-1] = "Destroy at least four of the five targets, then recover to the carrier."
    return spec, None


def run_template(fn, m: Map, out: Path, spec: dict):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = fn(m.cfg_path, m.build, m.prefab, str(out), spec)
        err = None
    except Exception as e:  # noqa: BLE001 - one mission failing must not stop the others or the build
        code, err = 1, f"{type(e).__name__}: {e}"
    text = buf.getvalue().rstrip()
    if text:
        print("  " + text.replace("\n", "\n  "))
    if code == 0 and (out / spec["title"]).is_dir():
        return None
    if err:
        return err
    probs = re.findall(r"problems: (.*)", text)
    return probs[-1][:300] if probs else "it did not pass the mission checks"


def main(cfg_path, build, prefab, out, name):
    out = Path(out)
    m = Map(cfg_path, build, prefab)
    if m.bare:
        print(f"bare airbases (no hangars, nobody spawns there): {', '.join(sorted(m.bare))}")
    jobs = []
    spec, why = escalation_spec(m, name)
    jobs.append(("Escalation", scenarios.escalation, spec, why))
    for att in SIDES:
        spec, why = strike_spec(m, name, att)
        jobs.append((f"{att} Strike", scenarios.strike, spec, why))
    made, skipped = [], []
    for kind, fn, spec, why in jobs:
        if spec is None:
            if not why.endswith("has no airbase to strike"):
                skipped.append(f"{kind} ({why})")
            continue
        print(f"\n-- {kind}: {spec['title']}")
        why = run_template(fn, m, out, spec)
        if why:
            skipped.append(f"{kind} ({why})")
        else:
            made.append(spec["title"])
    for s in skipped:
        print(f"NOTE: Left out the {s}.")
    print(json.dumps({"made": made, "skipped": skipped}))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
