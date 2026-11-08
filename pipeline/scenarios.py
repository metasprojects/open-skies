"""Mission templates for any Open Skies map, driven by maps/<map>/missions.json.

Every template builds through missions.MissionBuilder, so every unit goes through placement.MapGround and every
mission through the strict mission_lint before it is written. Game rules these rely on are in
docs/MISSION-PLAYBOOK.md (one trigger per route leg, destroy objectives only count spawned units, and so on).

Templates:
  strike      one side flies a strike on defended targets; the other side is AI only. Win: destroy enough of
              the targets. Lose: the time limit runs out.
  escalation  PvP (or co-op against AI): every airbase is capturable, each side has a factory complex at its
              main base and SAM rings at every base; a side wins by capturing all enemy bases and destroying
              the enemy factories (sinking the enemy carriers where a side has no airfield).
"""
import json
import math
from pathlib import Path

from mapgeo import MapGeo
from missions import ALL_AIRCRAFT, MissionBuilder
from placement import MapGround

SIDES = ("Boscali", "Primeva")
TAG = {"Boscali": "BDF", "Primeva": "PALA"}
ESCALATION_SUPPLIES = {"COIN": 8, "AttackHelo1": 2, "UtilityHelo1": 2, "QuadVTOL1": 2, "VTOLTrainer1": 2, "trainer": 2}


def other(side):
    return SIDES[1 - SIDES.index(side)]


# Map names that read with "the": "the Taiwan Strait", "the Strait of Hormuz", "the Falkland Islands"; but "Crete",
# "Lofoten and Narvik".
_THE_LAST = {"strait", "straits", "peninsula", "islands", "isles", "sea", "gulf", "bay", "channel", "coast",
             "archipelago", "alps", "highlands", "mountains", "valley", "lakes", "sound", "delta", "keys", "balkans"}


def place_name(name):
    words = name.split()
    if not words or words[0].lower() == "the":
        return name
    if words[-1].lower() in _THE_LAST or (len(words) > 2 and words[1].lower() == "of"):
        return "the " + name
    return name


class Ctx:
    """One mission being built on one map: geography, ground checks, airbases and the builder."""

    def __init__(self, cfg_path, build, prefab, description, time=7.0, weather=0.2, rank=3, bare_bases_ok=False):
        # bare_bases_ok: an airbase with no room for even one hangar is left bare with a note (no aircraft spawn
        # there) instead of failing the whole mission. Open Skies sets it for maps nobody has looked at; hand-checked
        # maps can keep the hard failure.
        self.bare_bases_ok = bare_bases_ok
        self.geo = MapGeo.load(cfg_path)
        self.cfg = self.geo.cfg
        self.g = MapGround(build, self.cfg)
        self.bases = {a["uniqueName"]: a for a in json.loads(Path(build, "airbases.json").read_text(encoding="utf-8"))}
        self.mb = MissionBuilder(self.g, prefab, description, time=time, weather=weather, rank=rank)

    def at(self, lat, lon, dx=0.0, dz=0.0):
        x, z = self.geo.game_xz(lat, lon)
        return x + dx, z + dz

    def side_bases(self, side):
        return [a for a in self.bases.values() if a.get("faction") == side]

    def short(self, base):
        a = self.bases[base]
        return a.get("displayName") or base

    # --- building blocks ---
    def flex_vehicle(self, utype, fac, x, z, yaw, name, cx=None, cz=None):
        """A defence unit that may move closer in, or be left out, where the ground will not take it (small
        islands like Nangan): tried at its spot, then halfway to the site centre, then skipped with a note."""
        cx, cz = (x, z) if cx is None else (cx, cz)
        for px, pz in ((x, z), ((x + cx) / 2, (z + cz) / 2), (cx, cz)):
            if self.g.find_ground(px, pz, radius=400) is not None:
                return self.mb.vehicle(utype, fac, px, pz, yaw=yaw, name=name, search=400)
        print(f"  note: left out {name} ({utype}): no ground for it near ({x:.0f}, {z:.0f})")
        return None

    def sam_site(self, fac, tag, x, z, radar=1, launchers=4, aaa=2, r=900):
        units = [self.flex_vehicle("RadarSAM1", fac, x + 150 * k, z, 0.0, f"{tag}_Radar") for k in range(radar)]
        for utype, n, rr, label in (("SAMTrailer1", launchers, r, "Launcher"), ("SPAAG1", aaa, r * 1.6, "AAA")):
            for k in range(n):
                a = 2 * math.pi * k / max(1, n)
                units.append(self.flex_vehicle(utype, fac, x + rr * math.sin(a), z + rr * math.cos(a),
                                               math.degrees(a), f"{tag}_{label}", x, z))
        return [u for u in units if u]

    def garrison(self, fac, name, tag):
        """A small mechanised garrison beside an airbase, so taking it needs a fight on the ground too."""
        x, z = self.beside_base(name, extra=1500.0, radius=400.0)
        out = []
        for k, t in enumerate(("MBT1" if fac == "Boscali" else "MBT", "MBT1" if fac == "Boscali" else "MBT",
                               "Linebreaker_IFV", "AFV8_IFV", "SPAAG2")):
            out.append(self.flex_vehicle(t, fac, x + 90 * k, z + 60 * (k % 2), 0.0, f"{tag}_Garrison", x, z))
        return [u for u in out if u]

    def beside_base(self, name, extra=700.0, radius=900.0):
        """A point beside the airfield, on whichever side of the runway has the most usable land."""
        a = self.bases[name]
        best, score = None, -1
        for side in (90, -90, 0, 180):
            s, c = math.sin(math.radians(a["heading"] + side)), math.cos(math.radians(a["heading"] + side))
            d = (a["flatHalfAcross"] if side in (90, -90) else a["flatHalfAlong"]) + extra
            cx, cz = a["x"] + s * d, a["z"] + c * d
            ok = sum(self.g.ground_problem(cx + radius * math.sin(t), cz + radius * math.cos(t), building_clear=0) is None
                     for t in [k * math.pi / 6 for k in range(12)])
            if ok > score:
                best, score = (cx, cz), ok
        return best

    def base_ring(self, fac, name, tag, launchers=3, aaa=2):
        return self.sam_site(fac, tag, *self.beside_base(name), launchers=launchers, aaa=aaa, r=700)

    def target_group(self, fac, spec, tag):
        """spec: {"type": unit or building type, "lat", "lon" | "base": airbase name, "extra": metres beside the
        runway, "count": n, "spacing": m, "name": label}"""
        if "base" in spec:
            x, z = self.beside_base(spec["base"], extra=float(spec.get("extra", 1200.0)), radius=400.0)
        else:
            x, z = self.at(spec["lat"], spec["lon"], spec.get("dx", 0.0), spec.get("dz", 0.0))
        t, n, sp = spec["type"], int(spec.get("count", 1)), float(spec.get("spacing", 120.0))
        name = spec.get("name", t).replace(" ", "")
        out = []
        for k in range(n):
            a = 2 * math.pi * k / max(1, n)
            px, pz = (x, z) if n == 1 else (x + sp * math.sin(a), z + sp * math.cos(a))
            if t in BUILDING_TYPES:
                out.append(self.mb.building(t, fac, px, pz, yaw=math.degrees(a), name=f"{tag}_{name}"))
            else:
                out.append(self.mb.vehicle(t, fac, px, pz, yaw=math.degrees(a), name=f"{tag}_{name}"))
        return [u for u in out if u]

    def carrier_group(self, fac, near, tag, kinds=("FleetCarrier1", "AssaultCarrier1", "Destroyer1", "Destroyer1")):
        from make_freeflight import open_water
        p = open_water(self.g, *near, edge=self.cfg.get("edgeFadeMetres", 9000) + 6000.0)
        if not p:
            self.mb.problems.append(f"no open water for the {fac} carrier group")
            return []
        return self.mb.fleet(list(kinds), fac, p[0], p[1], 0.0, f"{tag}_Group", hold=True, spacing=1500)

    def capturable_airbases(self):
        for a in self.bases.values():
            self.mb.m["airbases"].append({
                "IsOverride": True, "faction": a["faction"], "UniqueName": a["uniqueName"],
                "DisplayName": a.get("displayName") or a["uniqueName"], "Disabled": False, "Capturable": True,
                "CaptureDefense": 10.0, "CaptureRange": 2500.0,
                "Center": {"x": a["x"], "y": a["y"], "z": a["z"]}, "SelectionPosition": {"x": a["x"], "y": a["y"], "z": a["z"]},
                "Tower": "", "VerticalLandingPoints": [], "ServicePoints": [], "roads": {"roads": []}, "runways": []})

    def mission_start(self, objectives):
        go = self.mb.start("Start_All", objectives)
        self.mb.m["objectives"].insert(0, {"Type": "None", "UniqueName": "Mission Start", "Faction": "",
                                           "DisplayName": "", "Hidden": True, "Outcomes": [go]})

    def write(self, out, title):
        self.mb.furnish_airbases(list(self.bases.values()), bare_ok=self.bare_bases_ok)
        problems = self.mb.check()
        counts = {k: len(self.mb.m[k]) for k in ("vehicles", "ships", "buildings", "aircraft")}
        print(f"{title}: units {counts}, objectives {len(self.mb.m['objectives'])}, outcomes {len(self.mb.m['outcomes'])}")
        if problems:
            print("  problems:", problems[:10])
            return 1
        print("  written to", self.mb.write(out, title))
        return 0


BUILDING_TYPES = {"radarStation1", "ammoDump", "ammunitionBunker", "fuelTank1", "storageTank", "hangar_med", "shelter1",
                  "revetment1", "controlTower1", "factory_large", "factory_tall", "VehicleDepot1", "refinery_main",
                  "pillbox", "guardTower1", "gabionBunker1", "Emplacement1_23mm", "Emplacement1_MANPADS",
                  "Emplacement1_ATGM", "Emplacement1_MG", "Helipad", "enrichmentPlant1"}


# ------------------------------------------------------------------------------------------------ strike
def strike(cfg, build, prefab, out, spec):
    """spec: title, attacker, description, time, weather, minutes, percent, targets [..], sams [..],
    baseRings [airbase names], intel [radio lines], ai {side: n}."""
    att = spec["attacker"]
    dfn = other(att)
    c = Ctx(cfg, build, prefab, spec["description"], bare_bases_ok=spec.get("bareBasesOk", False), time=spec.get("time", 6.5), weather=spec.get("weather", 0.25),
            rank=spec.get("rank", 3))
    mb = c.mb
    ai = spec.get("ai", {})
    mb.faction(att, balance=6000.0, income=5.0, join=200.0, warheads=0, ai_aircraft=ai.get(att, 2),
               supplies={t: 20 for t in ALL_AIRCRAFT})
    mb.faction(dfn, balance=6000.0, income=5.0, join=0.0, warheads=0, ai_aircraft=ai.get(dfn, 4),
               supplies={t: 20 for t in ALL_AIRCRAFT}, prevent_join=True)
    targets = []
    for k, t in enumerate(spec["targets"]):
        targets += c.target_group(dfn, t, f"TGT{k}")
    for k, s in enumerate(spec.get("sams", [])):
        x, z = c.at(s["lat"], s["lon"])
        c.sam_site(dfn, f"{TAG[dfn]}_SAM{k}", x, z, radar=s.get("radar", 1), launchers=s.get("launchers", 4),
                   aaa=s.get("aaa", 2), r=s.get("radius", 900))
    for k, b in enumerate(spec.get("baseRings", [])):
        c.base_ring(dfn, b, f"{TAG[dfn]}_Base{k}")
    if spec.get("carrier"):
        x, z = c.at(*spec["carrier"])
        c.carrier_group(att, (x, z), TAG[att], kinds=("FleetCarrier1", "Destroyer1"))

    o = mb.objective
    minutes = float(spec.get("minutes", 40))
    win = mb.end("Strike Victory", "Victory")
    lose = mb.end("Out of Time", "Defeat")
    hit = o("DestroyUnits", "Strike", att, display=spec.get("display", "Destroy the targets"),
            outcomes=[mb.message("Msg_Hit", spec.get("winText", "Targets destroyed. Head home."), False), win],
            targetUnits=targets, completeOrder="CompleteSome", completeSomePercent=float(spec.get("percent", 0.8)))
    clock = o("WaitSeconds", "Time Limit", att, hidden=True, seconds=minutes * 60,
              outcomes=[mb.message("Msg_OutOfTime", "Out of time. The targets have dispersed.", False), lose])
    lines = [mb.timed_message(att, 5 + 60 * k, text, f"I{k}") for k, text in enumerate(spec.get("intel", []))]
    lines.append(mb.timed_message(att, max(60.0, minutes * 60 - 300), "Five minutes left on the strike window.", "Late"))
    c.mission_start([hit, clock] + lines)
    return c.write(out, spec["title"])


# ------------------------------------------------------------------------------------------------ escalation
def escalation(cfg, build, prefab, out, spec):
    """spec: title, description, time, weather, carriers {side: [lat, lon]} for a side without airbases,
    factories {side: airbase name} (default: that side's first airbase)."""
    c = Ctx(cfg, build, prefab, spec["description"], bare_bases_ok=spec.get("bareBasesOk", False), time=spec.get("time", 7.0), weather=spec.get("weather", 0.2),
            rank=0)
    mb = c.mb
    for s in SIDES:
        mb.faction(s, balance=1000.0, income=2.0, join=45.0, warheads=16, reserve_warheads=16,
                   ai_aircraft=spec.get("ai", 4), supplies=ESCALATION_SUPPLIES, kill_reward=3.0)
    c.capturable_airbases()
    factories, carriers = {}, {}
    for s in SIDES:
        bases = c.side_bases(s)
        for k, b in enumerate(bases):
            tag = f"{TAG[s]}_{b.get('displayName', 'Base').replace(' ', '')}"
            c.base_ring(s, b["uniqueName"], tag, launchers=3 if k == 0 else 2, aaa=2)
            c.garrison(s, b["uniqueName"], tag)
        if bases:
            main = spec.get("factories", {}).get(s, bases[0]["uniqueName"])
            fx, fz = c.beside_base(main, extra=2600.0, radius=500.0)
            f = [mb.building("VehicleDepot1", s, fx, fz, name=f"{TAG[s]}_Depot"),
                 mb.building("VehicleDepot1", s, fx + 350, fz + 200, name=f"{TAG[s]}_Depot"),
                 mb.building("factory_large", s, fx - 300, fz + 400, name=f"{TAG[s]}_Factory")]
            factories[s] = [u for u in f if u]
            mb.vehicle("Linebreaker_SAM", s, fx + 600, fz - 300, name=f"{TAG[s]}_Factory_SAM")
            mb.vehicle("SPAAG2", s, fx - 500, fz - 200, name=f"{TAG[s]}_Factory_AAA")
        else:
            lat, lon = spec["carriers"][s]
            carriers[s] = c.carrier_group(s, c.at(lat, lon), TAG[s])
    o = mb.objective
    starts = []
    for s in SIDES:
        e = other(s)
        goals = []
        for b in c.side_bases(e):
            goals.append(o("CaptureAirbase", f"{TAG[s]} Capture {c.short(b['uniqueName'])}", s,
                           display=f"Capture {c.short(b['uniqueName'])}", targetAirbases=[b["uniqueName"]],
                           completeOrder="CompleteAll", completeSomePercent=0.5))
        if factories.get(e):
            goals.append(o("DestroyUnits", f"{TAG[s]} Destroy Factories", s, display=f"Destroy the {e} factories",
                           targetUnits=factories[e], completeOrder="CompleteAll", completeSomePercent=0.5))
        if carriers.get(e):
            goals.append(o("DestroyUnits", f"{TAG[s]} Sink Carriers", s, display=f"Sink the {e} carriers",
                           targetUnits=[u for u in carriers[e] if "Carrier" in u], completeOrder="CompleteAll",
                           completeSomePercent=0.5))
        win = mb.end(f"{TAG[s]} Victory", "Victory")
        v = o("CompleteOtherObjective", f"{TAG[s]} Victory Conditions", s, hidden=True,
              outcomes=[mb.message(f"Msg_{TAG[s]}_Wins", f"{s} holds {place_name(c.cfg['displayName'])}.", False), win],
              targetObjectives=goals, completeOrder="CompleteAll", completeSomePercent=0.5)
        starts += goals + [v]
        starts.append(mb.timed_message(s, 5, spec.get("brief", {}).get(s, f"Take every {e} airbase and destroy "
                                       f"their factories. Income grows with every base you hold."), f"{TAG[s]}1"))
    c.mission_start(starts)
    return c.write(out, spec["title"])


# ------------------------------------------------------------------------------------------------ landing
def legs_for(points, start, speed):
    """Seconds per route leg: a little under the travel time at the speeds measured in the headless runs
    (ground columns ~15 m/s on roads, fleets ~15 m/s, landing craft ~34 m/s), so the move stays continuous."""
    out, (px, pz) = [], start
    for x, z in points:
        out.append(max(30.0, 0.9 * math.hypot(x - px, z - pz) / speed))
        px, pz = x, z
    return out


def landing(cfg, build, prefab, out, spec):
    """An amphibious assault built like Strait Crisis - Landfall (Taiwan), the template that passed the
    40-minute headless run: the fleet sails in two legs, landing craft launch from it at minute 14, land the
    first wave at minute 20, go back and bring the second at minute 38; each landing is cancelled if the
    defender sinks 6 of the 8 craft first. Attacker wins by capturing the target airbase, defender by sinking
    both assault carriers.

    spec: title, description, attacker, target (airbase), fleetStart, landingZone (sea off the beach), beach
    (shore point), beachhead (land where the waves come ashore), defences [[lat, lon] land points for beach
    groups], reserve {from, to}, defenderSams [{lat, lon, launchers}], defenderBaseRings, attackerBaseRings,
    attackerSams, navy {at: [lat, lon], ships: [...]}, text {...}."""
    A = spec["attacker"]
    D = other(A)
    ta, td = TAG[A], TAG[D]
    c = Ctx(cfg, build, prefab, spec["description"], bare_bases_ok=spec.get("bareBasesOk", False), time=spec.get("time", 6.6), weather=spec.get("weather", 0.22))
    mb, g = c.mb, c.g
    mb.faction(D, balance=8000.0, income=10.0, join=150.0, ai_aircraft=spec.get("aiDefender", 8),
               supplies={t: 30 for t in ALL_AIRCRAFT})
    mb.faction(A, balance=10000.0, income=10.0, join=150.0, ai_aircraft=spec.get("aiAttacker", 5),
               supplies={t: 30 for t in ALL_AIRCRAFT})
    text = spec.get("text", {})
    target = spec["target"]
    tb = c.bases[target]

    # ---- defender
    for k, s in enumerate(spec.get("defenderSams", [])):
        c.sam_site(D, f"{td}_SAM{k}", *c.at(s["lat"], s["lon"]), radar=s.get("radar", 1),
                   launchers=s.get("launchers", 4), aaa=s.get("aaa", 2), r=s.get("radius", 900))
    for k, b in enumerate(spec.get("defenderBaseRings", [])):
        c.base_ring(D, b, f"{td}_Base{k}")
    beach = []
    hd = spec.get("defenceHeading", 0.0)
    for k, (lat, lon) in enumerate(spec["defences"]):
        bx, bz = c.at(lat, lon)
        beach += mb.column("MBT1", D, bx, bz, hd, 3, 220, f"{td}_Beach{k}_Tank")
        beach += mb.column("Linebreaker_IFV", D, bx + 600, bz - 400, hd, 3, 220, f"{td}_Beach{k}_IFV")
        beach += mb.column("AFV8_IFV", D, bx - 700, bz + 500, hd, 3, 220, f"{td}_Beach{k}_IFV2")
        beach.append(c.flex_vehicle("SPAAG1", D, bx + 700, bz - 700, 0.0, f"{td}_Beach{k}_AAA", bx, bz))
        beach.append(c.flex_vehicle("LaserTrailer1", D, bx + 900, bz - 400, 0.0, f"{td}_Beach{k}_Laser", bx, bz))
        c.flex_vehicle("CRAMTrailer1", D, bx + 500, bz - 900, 0.0, f"{td}_Beach{k}_CRAM", bx, bz)
        c.flex_vehicle("LaserTrailer1", D, bx - 900, bz + 300, 0.0, f"{td}_Beach{k}_Laser2", bx, bz)
        beach.append(mb.building("Emplacement1_ATGM", D, bx - 300, bz + 300, name=f"{td}_Beach{k}_ATGM"))
        beach.append(mb.building("pillbox", D, bx - 250, bz + 450, name=f"{td}_Beach{k}_Pillbox"))
    beach = [u for u in beach if u]
    if spec.get("navy"):
        mb.fleet(spec["navy"]["ships"], D, *c.at(*spec["navy"]["at"]), 0.0, f"{td}_Navy", hold=True, spacing=1200)
    res_leg0 = None
    if spec.get("reserve"):
        hx, hz = c.at(*spec["reserve"]["from"])
        tx, tz = c.at(*spec["reserve"]["to"])
        pts = [(hx + (tx - hx) * f, hz + (tz - hz) * f) for f in (0.35, 0.7, 1.0)]
        res_route, res_leg0 = mb.route(f"{td} Reserve", D, pts, legs_for(pts, (hx, hz), 12.0))
        mb.column("MBT1", D, hx, hz, 0.0, 4, 90, f"{td}_Reserve_Tank", hold=False, waypoints=res_route)
        mb.column("AFV8_IFV", D, hx - 300, hz - 300, 0.0, 4, 90, f"{td}_Reserve_IFV", hold=False, waypoints=res_route)
        mb.column("Linebreaker_SAM", D, hx - 600, hz - 600, 0.0, 2, 120, f"{td}_Reserve_SAM", hold=False,
                  waypoints=res_route)

    # ---- attacker
    fx, fz = c.at(*spec["fleetStart"])
    lzx, lzz = c.at(*spec["landingZone"])
    if spec.get("fleetRoute"):
        # Through a fjord or sound: the route's own points (open water), ending at the landing zone.
        pts = [c.at(*p) for p in spec["fleetRoute"]] + [(lzx, lzz)]
        midx, midz = pts[-2]
    else:
        midx, midz = fx + (lzx - fx) * 0.5, fz + (lzz - fz) * 0.5
        pts = [(midx, midz), (lzx, lzz)]
    fleet_route, fleet_leg0 = mb.route(f"{ta} Fleet", A, pts, legs_for(pts, (fx, fz), 15.0))
    heading = math.degrees(math.atan2(lzx - fx, lzz - fz)) % 360
    fleet = mb.fleet(spec.get("fleet", ["AssaultCarrier1", "AssaultCarrier1", "FleetCarrier1", "Destroyer1", "Destroyer1",
                                        "Destroyer1", "Frigate1", "Frigate1", "Frigate1", "Corvette1", "Corvette1"]),
                     A, fx, fz, heading, f"{ta}_Fleet", waypoints=fleet_route, hold=False,
                     spacing=spec.get("fleetSpacing", 1400))
    carriers = [u for u in fleet if "AssaultCarrier1" in u]
    gx, gz = c.at(*spec["beach"])
    ux, uz = lzx - gx, lzz - gz
    un = math.hypot(ux, uz) or 1.0
    ux, uz = ux / un, uz / un
    beach_pt = None
    for d in range(100, 12000, 50):
        x, z = gx + ux * d, gz + uz * d
        if -g.height(x, z) >= 4.0 and g.shore_distance(x, z) >= spec.get("craftStandoff", 400):
            beach_pt = (x, z)
            break
    home = (g.find_sea(*c.at(*spec["craftHome"]), "LandingCraft1") if spec.get("craftHome")
            else g.find_sea(midx + 1500 * uz, midz - 1500 * ux, "LandingCraft1"))
    if not beach_pt or not home:
        mb.problems.append("no landing-craft approach point off the beach, or no water beside the fleet for them")
        return c.write(out, spec["title"])
    tm = spec.get("timing", {})
    LAUNCH, W1 = tm.get("launch", 840.0), tm.get("wave1", 1200.0)
    AGAIN, W2 = tm.get("again", 1900.0), tm.get("wave2", 2280.0)
    craft_route, craft_leg0 = mb.route(f"{ta} Craft", A, [beach_pt, home, beach_pt],
                                       [W1 + 30 - LAUNCH, AGAIN - W1 - 30, 30.0])
    craft = mb.fleet(["LandingCraft1"] * 8, A, home[0], home[1], heading, f"{ta}_Landing", hold=False,
                     spacing=min(500, spec.get("fleetSpacing", 1400) / 2),
                     waypoints=craft_route)
    for k, s in enumerate(spec.get("attackerSams", [])):
        c.sam_site(A, f"{ta}_SAM{k}", *c.at(s["lat"], s["lon"]), launchers=s.get("launchers", 4), aaa=s.get("aaa", 2))
    for k, b in enumerate(spec.get("attackerBaseRings", [])):
        c.base_ring(A, b, f"{ta}_Base{k}")
    bhx, bhz = c.at(*spec["beachhead"])
    pts = [(bhx + (tb["x"] - bhx) * f, bhz + (tb["z"] - bhz) * f) for f in (0.4, 0.8, 1.0)]
    push1, w1_leg0 = mb.route(f"{ta} Wave1", A, pts, legs_for(pts, (bhx, bhz), 12.0))
    push2, w2_leg0 = mb.route(f"{ta} Wave2", A, pts, legs_for(pts, (bhx, bhz), 12.0))
    wh = math.degrees(math.atan2(tb["x"] - bhx, tb["z"] - bhz)) % 360
    wave1 = (mb.column("MBT", A, bhx, bhz, wh, 6, 80, f"{ta}_Wave1_Tank", hold=False, waypoints=push1)
             + mb.column("AFV8_IFV", A, bhx + 300, bhz - 200, wh, 6, 80, f"{ta}_Wave1_IFV", hold=False, waypoints=push1))
    wave2 = (mb.column("MBT", A, bhx - 400, bhz + 300, wh, 6, 80, f"{ta}_Wave2_Tank", hold=False, waypoints=push2)
             + mb.column("Linebreaker_APC", A, bhx - 100, bhz + 500, wh, 6, 80, f"{ta}_Wave2_APC", hold=False,
                         waypoints=push2)
             + mb.column("AFV8_SAM", A, bhx - 700, bhz + 100, wh, 2, 100, f"{ta}_Wave2_SAM", hold=False, waypoints=push2))
    mb.m["airbases"].append({
        "IsOverride": True, "faction": D, "UniqueName": target, "DisplayName": c.short(target),
        "Disabled": False, "Capturable": True, "CaptureDefense": 20.0, "CaptureRange": 2500.0,
        "Center": {"x": tb["x"], "y": tb["y"], "z": tb["z"]}, "SelectionPosition": {"x": tb["x"], "y": tb["y"], "z": tb["z"]},
        "Tower": "", "VerticalLandingPoints": [], "ServicePoints": [], "roads": {"roads": []}, "runways": []})

    # ---- script
    o = mb.objective
    tname = c.short(target)
    win_a, win_d = mb.end(f"{A} Victory", "Victory"), mb.end(f"{D} Victory", "Victory")
    take = o("CaptureAirbase", f"Take {tname}", A, display=f"Capture {tname}",
             outcomes=[mb.message("Msg_Taken", f"{tname} has fallen. The beachhead is secure.", False), win_a],
             targetAirbases=[target], completeOrder="CompleteAll", completeSomePercent=0.5)
    beach_o = o("DestroyUnits", "Clear the Beaches", A, display="Destroy the beach defences",
                outcomes=[mb.message("Msg_Beaches", "Beach defences broken. The landing craft have a clear run in.")],
                targetUnits=beach, completeOrder="CompleteSome", completeSomePercent=0.6)
    sink = o("DestroyUnits", "Sink the Assault Carriers", D, display="Sink both assault carriers",
             outcomes=[mb.message("Msg_Sunk", "Both assault carriers are going down. The landing has failed.", False), win_d],
             targetUnits=carriers, completeOrder="CompleteAll", completeSomePercent=0.5)
    stop1 = o("DestroyUnits", "Stop the Landing Craft", D, display="Sink the landing craft before they reach the beach",
              outcomes=[mb.stop("Cancel_Wave1", ["Wave 1 Lands"]),
                        mb.message("Msg_Craft", "Landing craft destroyed. The first wave never made it ashore.", False)],
              targetUnits=craft, completeOrder="CompleteSome", completeSomePercent=0.75)
    stop2 = o("DestroyUnits", "Stop the Second Landing", D, display="Sink the landing craft before the second wave lands",
              outcomes=[mb.stop("Cancel_Wave2", ["Wave 2 Lands"]),
                        mb.message("Msg_Craft2", "Not enough landing craft left. The second wave is not coming.", False)],
              targetUnits=craft, completeOrder="CompleteSome", completeSomePercent=0.75)
    launch = o("WaitSeconds", "Craft Launch", A, hidden=True, seconds=LAUNCH,
               outcomes=[mb.spawn("Spawn_Craft", craft), mb.start("Craft_Go", [craft_leg0, stop1])])
    again = o("WaitSeconds", "Craft Go Again", A, hidden=True, seconds=AGAIN, outcomes=[mb.start("Craft_Again", [stop2])])
    w1 = o("WaitSeconds", "Wave 1 Lands", A, hidden=True, seconds=W1,
           outcomes=[mb.spawn("Spawn_Wave1", wave1), mb.start("Wave1_Go", [w1_leg0]), mb.stop("Wave1_Ashore", [stop1]),
                     mb.message("Msg_Wave1", f"First wave ashore. Armour is pushing for {tname}.", False)])
    w2 = o("WaitSeconds", "Wave 2 Lands", A, hidden=True, seconds=W2,
           outcomes=[mb.spawn("Spawn_Wave2", wave2), mb.start("Wave2_Go", [w2_leg0]), mb.stop("Wave2_Ashore", [stop2]),
                     mb.message("Msg_Wave2", "Second wave ashore with air defence. Hold the beachhead.", False)])
    starts = [take, beach_o, sink, launch, again, w1, w2, fleet_leg0]
    if res_leg0:
        starts.append(o("WaitSeconds", f"{td} Reserve Moves", D, hidden=True, seconds=600.0,
                        outcomes=[mb.start("Reserve_Go", [res_leg0]),
                                  mb.message("Msg_Reserve", text.get("reserve", "The reserve is moving up. Cover its route."))]))
    lines = [(A, 5, text.get("a1", "The fleet is closing the coast. Clear the beach defences before the craft go in.")),
             (A, LAUNCH + 2, "Landing craft away. First wave ashore in six minutes; keep the beaches quiet."),
             (A, AGAIN + 2, "Landing craft going in again with the second wave."),
             (D, 5, text.get("d1", "Invasion fleet sighted. All units to the coast.")),
             (D, 90, "The two assault carriers carry the landing force. Sink them."),
             (D, LAUNCH + 2, "Landing craft launched from the fleet. Sink them before they reach the beach."),
             (D, AGAIN + 2, "The landing craft are heading back in. Stop the second wave.")]
    starts += [mb.timed_message(s, t, line, f"{TAG[s]}{k}") for k, (s, t, line) in enumerate(lines)]
    c.mission_start(starts)
    return c.write(out, spec["title"])


# ------------------------------------------------------------------------------------------------ offensive
def offensive(cfg, build, prefab, out, spec):
    """A ground offensive across a front line (Korea's Imjin). The attacker sends three armoured waves from
    assembly areas, each by its own crossing to a stretch of the defender's line, with artillery and a SAM
    umbrella behind; the defender holds a line of strongpoints with a reserve that moves up at minute 10.
    Attacker wins by breaking 70 % of the line; defender wins by destroying 75 % of all three waves.

    spec: title, description, attacker, line [[lat, lon] strongpoints], waves [{assembly: [lat, lon],
    crossing: [lat, lon], target: index into line, at: seconds}], artillery [lat, lon], attackerSams,
    defenderSams, defenderBaseRings, attackerBaseRings, reserve {from, to}, lineHeading, text {...}."""
    A = spec["attacker"]
    D = other(A)
    ta, td = TAG[A], TAG[D]
    c = Ctx(cfg, build, prefab, spec["description"], bare_bases_ok=spec.get("bareBasesOk", False), time=spec.get("time", 6.4), weather=spec.get("weather", 0.3))
    mb = c.mb
    mb.faction(D, balance=8000.0, income=10.0, join=150.0, ai_aircraft=spec.get("aiDefender", 6),
               supplies={t: 30 for t in ALL_AIRCRAFT})
    mb.faction(A, balance=10000.0, income=10.0, join=150.0, ai_aircraft=spec.get("aiAttacker", 6),
               supplies={t: 30 for t in ALL_AIRCRAFT})
    text = spec.get("text", {})
    for k, s in enumerate(spec.get("defenderSams", [])):
        c.sam_site(D, f"{td}_SAM{k}", *c.at(s["lat"], s["lon"]), launchers=s.get("launchers", 4), aaa=s.get("aaa", 2))
    for k, b in enumerate(spec.get("defenderBaseRings", [])):
        c.base_ring(D, b, f"{td}_Base{k}")
    for k, s in enumerate(spec.get("attackerSams", [])):
        c.sam_site(A, f"{ta}_SAM{k}", *c.at(s["lat"], s["lon"]), launchers=s.get("launchers", 4), aaa=s.get("aaa", 2))
    for k, b in enumerate(spec.get("attackerBaseRings", [])):
        c.base_ring(A, b, f"{ta}_Base{k}")

    # Defender line: one strongpoint per point, facing the attacker.
    hd = spec.get("lineHeading", 0.0)
    line, line_xy = [], []
    for k, (lat, lon) in enumerate(spec["line"]):
        x, z = c.at(lat, lon)
        line_xy.append((x, z))
        line += mb.column("MBT1", D, x, z, hd, 3, 200, f"{td}_Line{k}_Tank")
        line += mb.column("Linebreaker_IFV", D, x + 500, z - 300, hd, 3, 200, f"{td}_Line{k}_IFV")
        line.append(c.flex_vehicle("LightTruck1_AT", D, x - 400, z + 200, hd, f"{td}_Line{k}_AT", x, z))
        line.append(c.flex_vehicle("LightTruck1_AT", D, x - 600, z - 100, hd, f"{td}_Line{k}_AT", x, z))
        line.append(c.flex_vehicle("SPAAG2", D, x + 200, z - 700, hd, f"{td}_Line{k}_AAA", x, z))
        line.append(mb.building("Emplacement1_ATGM", D, x - 200, z + 300, name=f"{td}_Line{k}_ATGM"))
        line.append(mb.building("pillbox", D, x + 300, z + 250, name=f"{td}_Line{k}_Pillbox"))
    line = [u for u in line if u]
    res_leg0 = None
    if spec.get("reserve"):
        hx, hz = c.at(*spec["reserve"]["from"])
        tx, tz = c.at(*spec["reserve"]["to"])
        pts = [(hx + (tx - hx) * f, hz + (tz - hz) * f) for f in (0.35, 0.7, 1.0)]
        res_route, res_leg0 = mb.route(f"{td} Reserve", D, pts, legs_for(pts, (hx, hz), 12.0))
        mb.column("MBT1", D, hx, hz, hd, 4, 90, f"{td}_Reserve_Tank", hold=False, waypoints=res_route)
        mb.column("AFV8_IFV", D, hx - 300, hz - 300, hd, 4, 90, f"{td}_Reserve_IFV", hold=False, waypoints=res_route)
        mb.column("Linebreaker_SAM", D, hx - 600, hz - 600, hd, 2, 120, f"{td}_Reserve_SAM", hold=False, waypoints=res_route)

    # Attacker artillery and waves.
    arty = []
    if spec.get("artillery"):
        ax, az = c.at(*spec["artillery"])
        arty = mb.column("Truck2-MLRS", A, ax, az, (hd + 180) % 360, 6, 150, f"{ta}_Artillery")
        c.flex_vehicle("Truck2-R", A, ax + 400, az, 0.0, f"{ta}_Artillery_Radar", ax, az)
    o = mb.objective
    waves_all, spawns = [], []
    for k, w in enumerate(spec["waves"]):
        sx, sz = c.at(*w["assembly"])
        cx, cz = c.at(*w["crossing"])
        tx, tz = line_xy[w.get("target", k % len(line_xy))]
        pts = [(cx, cz), (tx, tz)]
        route, leg0 = mb.route(f"{ta} Wave{k + 1}", A, pts, legs_for(pts, (sx, sz), 12.0))
        wh = math.degrees(math.atan2(cx - sx, cz - sz)) % 360
        units = (mb.column("MBT", A, sx, sz, wh, 5, 90, f"{ta}_Wave{k + 1}_Tank", hold=False, waypoints=route)
                 + mb.column("AFV8_IFV", A, sx + 300, sz - 200, wh, 5, 90, f"{ta}_Wave{k + 1}_IFV", hold=False, waypoints=route)
                 + mb.column("AFV8_SAM", A, sx - 300, sz + 200, wh, 1, 90, f"{ta}_Wave{k + 1}_SAM", hold=False,
                             waypoints=route))
        waves_all += units
        t = float(w.get("at", 120 + 900 * k))
        spawns.append(o("WaitSeconds", f"Wave {k + 1} Attacks", A, hidden=True, seconds=t,
                        outcomes=[mb.spawn(f"Spawn_Wave{k + 1}", units), mb.start(f"Wave{k + 1}_Go", [leg0]),
                                  mb.message(f"Msg_Wave{k + 1}", w.get("text", f"Wave {k + 1} is moving on the line."),
                                             False)]))
    win_a, win_d = mb.end(f"{A} Victory", "Victory"), mb.end(f"{D} Victory", "Victory")
    brk = o("DestroyUnits", "Break the Line", A, display=text.get("attackGoal", "Break the defence line"),
            outcomes=[mb.message("Msg_Broken", text.get("broken", "The line is broken. The road south is open."), False),
                      win_a],
            targetUnits=line, completeOrder="CompleteSome", completeSomePercent=0.7)
    hold = o("DestroyUnits", "Stop the Offensive", D, display=text.get("defendGoal", "Destroy the attacking armour"),
             outcomes=[mb.message("Msg_Held", text.get("held", "The offensive has burned out. The line holds."), False),
                       win_d],
             targetUnits=waves_all, completeOrder="CompleteSome", completeSomePercent=0.75)
    starts = [brk, hold] + spawns
    if arty:
        starts.append(o("DestroyUnits", "Silence the Artillery", D, display="Destroy the rocket artillery",
                        outcomes=[mb.message("Msg_Arty", "Enemy rocket artillery destroyed.", False)],
                        targetUnits=arty, completeOrder="CompleteSome", completeSomePercent=0.8))
    if res_leg0:
        starts.append(o("WaitSeconds", f"{td} Reserve Moves", D, hidden=True, seconds=600.0,
                        outcomes=[mb.start("Reserve_Go", [res_leg0]),
                                  mb.message("Msg_Reserve", text.get("reserve", "The reserve is moving up to the line."))]))
    for k, (s, t, line_text) in enumerate([(A, 5, text.get("a1", "The offensive begins. Cover the armour.")),
                                           (D, 5, text.get("d1", "Enemy armour is massing. Hold the line.")),
                                           (D, 90, text.get("d2", "Hit the armour at the crossings, and find their artillery."))]):
        starts.append(mb.timed_message(s, t, line_text, f"{TAG[s]}{k}"))
    c.mission_start(starts)
    return c.write(out, spec["title"])


TEMPLATES = {"strike": strike, "escalation": escalation, "landing": landing, "offensive": offensive}
