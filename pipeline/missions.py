"""Build Nuclear Option missions in the game's current format (JsonVersion 6, the layout of its own missions).

Rules learned from 86 real missions (docs/MISSION-PLAYBOOK.md):
  - objectives and outcomes are two top-level lists; objectives name outcomes, StartObjective outcomes name
    objectives; a hidden "Mission Start" objective begins everything
  - a unit listed in any SpawnUnit outcome waits for that outcome; every other unit is there at the start
  - a vehicle or ship waypoint names the objective that releases it ("Unit Spawn" = go as soon as it exists)
  - ships sit at fixed heights per type; ground units at ground height
Every placement goes through placement.MapGround so nothing is buried, floating, on a runway or in a building.
"""
import json
import math
from pathlib import Path

from placement import DRAFT, MapGround, validate, yaw_quat

SHIP_Y = {"Corvette1": 7.82, "SmallCarrier1": 6.25, "AssaultCarrier1": 5.78, "Frigate1": 6.31, "PatrolBoat1": 2.67,
          "FleetCarrier1": 5.71, "Destroyer1": 7.34, "LandingCraft1": 9.23}
# Units spawn at exactly this height above the terrain collider; the game does not snap them down.
GROUND_CLEARANCE = 0.3
# Rough footprint radius (m) of mission-placed buildings, and half the gap kept between two vehicles. A vehicle is
# never placed inside a mission building or on another vehicle: in the 2026-11-07 tests Black Buck's radar SAM had
# snapped onto the very spot of the radar station it guards (both searched outward to the same patch of ground) and
# sat 24 m inside it.
FOOTPRINT = {"radarStation1": 40.0, "hangar_med": 45.0, "shelter1": 25.0, "Helipad": 20.0, "ammoDump": 30.0,
             "ammunitionBunker": 25.0, "fuelTank1": 20.0, "storageTank": 20.0, "factory_large": 70.0,
             "factory_tall": 50.0}
VEHICLE_HALF_GAP = 6.0
ALL_AIRCRAFT = ["COIN", "trainer", "AttackHelo1", "Fighter1", "SmallFighter1", "QuadVTOL1", "Multirole1", "EW1",
                "Darkreach", "UtilityHelo1", "CAS1", "FastBomber1", "VTOLTrainer1"]


def _cap(v=0.0):
    return {"IsOverride": False, "Value": v}


def _cam():
    return {"IsOverride": False, "Value": {"Position": {"x": 0.0, "y": 0.0, "z": 0.0},
                                           "Rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 0.0}}}


class MissionBuilder:
    def __init__(self, ground: MapGround, prefab, description="", time=7.0, weather=0.2, rank=5):
        self.g = ground
        self.names = set()
        self.problems = []
        self.m = {
            "JsonVersion": 6,
            "MapKey": {"Type": "GameWorldPrefab", "Path": prefab},
            "missionSettings": {"description": description, "allowEventContent": False, "Tags": [],
                                "playerMode": "SingleAndMultiplayer", "allowRespawn": True, "playerStartingRank": rank,
                                "rankMultiplier": 1.0, "successfulSortieBonus": 0.25, "nuclearEscalationThreshold": 0.0,
                                "strategicEscalationThreshold": 0.0, "minRankTacticalWarhead": 0, "minRankStrategicWarhead": 0,
                                "cameraStartPosition": _cam(), "missionRoads": {"roads": []}, "missionSeaLanes": {"roads": []},
                                "wrecksMaxNumber": 0, "wrecksDecayTime": 0.0},
            "environment": {"timeOfDay": time, "timeFactor": 0.0, "weatherIntensity": weather, "cloudAltitude": 1800.0,
                            "windSpeed": 0.0, "windTurbulence": 0.0, "windHeading": 0.0, "windRandomHeading": 0.0, "moonPhase": 14.0},
            "aircraft": [], "vehicles": [], "ships": [], "buildings": [], "scenery": [], "containers": [], "missiles": [],
            "pilots": [], "factions": [], "airbases": [], "objectives": [], "outcomes": [],
        }

    # --- names ---
    def name(self, base):
        n, k = base, 1
        while n in self.names:
            n, k = f"{base}_{k}", k + 1
        self.names.add(n)
        return n

    # --- factions ---
    def faction(self, name, balance=10000.0, income=5.0, join=150.0, warheads=0, ai_aircraft=4, supplies=None,
                restricted=None, kill_reward=1.0, prevent_join=False, reserve_warheads=0):
        self.m["factions"].append({
            "factionName": name, "preventJoin": prevent_join, "preventDonation": False,
            "supplies": [{"UnitType": t, "Count": c} for t, c in (supplies or {}).items()],
            "startingBalance": balance, "playerJoinAllowance": join, "playerTaxRate": 0.2, "regularIncome": income,
            "excessFundsDistributePercent": 0.25, "killReward": kill_reward, "airSkillMultiplier": 1.0,
            "surfaceSkillMultiplier": 1.0, "startingWarheads": warheads, "reserveWarheads": reserve_warheads,
            "reserveAirframes": 1,
            "extraReservesPerPlayer": 1, "AIAircraftLimit": ai_aircraft, "reduceAIPerFriendlyPlayer": 1.0,
            "addAIPerEnemyPlayer": 1.0, "restrictions": {"aircraft": restricted or [], "weapons": []},
            "cameraStartPosition": _cam()})

    # --- units ---
    def _unit(self, kind, utype, faction, x, y, z, yaw, name, extra):
        u = {"type": utype, "faction": faction, "UniqueName": self.name(name or utype),
             "globalPosition": {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}, "rotation": yaw_quat(yaw),
             "CaptureStrength": _cap(), "CaptureDefense": _cap()}
        u.update(extra)
        self.m[kind].append(u)
        return u["UniqueName"]

    def clash(self, x, z, r, kinds=("buildings", "vehicles")):
        """What a new object of radius r at (x, z) would overlap among the buildings and vehicles placed so far."""
        for key in kinds:
            for u in self.m[key]:
                p = u["globalPosition"]
                ur = FOOTPRINT.get(u["type"], 30.0) if key == "buildings" else VEHICLE_HALF_GAP
                if (p["x"] - x) ** 2 + (p["z"] - z) ** 2 < (ur + r) ** 2:
                    return f"against {u['UniqueName']}"
        return None

    def vehicle(self, utype, faction, x, z, yaw=0.0, name=None, hold=True, skill=0.8, waypoints=None, search=600.0):
        # A vehicle with a route is always held. An unheld vehicle with no player order keeps re-aiming itself at
        # the nearest objective or enemy (seen in headless runs), which overrides mission waypoints: the
        # v0.2 Landfall reserve set off for the coast at minute 0 instead of waiting for minute 10. Held, it stays
        # put until a waypoint's objective starts and then drives the route (the built-in missions hold all 123
        # vehicles whose first waypoint waits for an objective).
        if waypoints:
            hold = True
        p = self.g.find(x, z, lambda a, b: self.g.ground_problem(a, b) or self.clash(a, b, VEHICLE_HALF_GAP), search)
        if p is None:
            self.problems.append(f"no valid ground for {name or utype} near ({x:.0f}, {z:.0f})")
            return None
        x, z = p
        return self._unit("vehicles", utype, faction, x, self.g.height(x, z) + GROUND_CLEARANCE, z, yaw, name,
                          {"holdPosition": hold, "skill": skill, "waypoints": waypoints or []})

    def building(self, utype, faction, x, z, yaw=0.0, name=None, airbase="", capturable=False, production=None,
                 search=600.0):
        r = FOOTPRINT.get(utype, 30.0)
        p = self.g.find(x, z, lambda a, b: self.g.ground_problem(a, b, max_slope=8.0) or self.clash(a, b, r), search)
        if p is None:
            self.problems.append(f"no valid ground for building {name or utype} near ({x:.0f}, {z:.0f})")
            return None
        x, z = p
        extra = {"capturable": capturable, "Airbase": airbase, "placementOffset": 0.0}
        if production:
            extra["factoryOptions"] = {"productionType": production[0], "productionTime": production[1]}
        return self._unit("buildings", utype, faction, x, self.g.height(x, z), z, yaw, name, extra)

    def ship(self, utype, faction, x, z, yaw=0.0, name=None, hold=True, skill=0.8, waypoints=None, search=8000.0):
        p = self.g.find_sea(x, z, utype, radius=search)
        if p is None:
            self.problems.append(f"no deep enough water for {name or utype} near ({x:.0f}, {z:.0f})")
            return None
        x, z = p
        return self._unit("ships", utype, faction, x, SHIP_Y.get(utype, 6.0), z, yaw, name,
                          {"holdPosition": hold, "skill": skill, "waypoints": waypoints or []})

    def aircraft_air(self, utype, faction, x, z, alt, yaw=0.0, name=None, speed=180.0, player=False, skill=0.9, loadout=None):
        y = max(self.g.height(x, z), 0) + alt
        return self._unit("aircraft", utype, faction, x, y, z, yaw, name, {
            "playerControlled": player, "playerControlledPriority": 0,
            "savedLoadout": {"Selected": [{"Key": k} for k in (loadout or [])]},
            "livery": 0, "liveryType": "Builtin", "liveryName": "", "fuel": 1.0, "skill": skill, "bravery": 0.5,
            "startingSpeed": speed})

    # --- formations ---
    def ring(self, utype, faction, cx, cz, radius, count, name, **kw):
        out = []
        for k in range(count):
            a = 2 * math.pi * k / count
            out.append(self.vehicle(utype, faction, cx + radius * math.sin(a), cz + radius * math.cos(a),
                                    yaw=math.degrees(a), name=name, **kw))
        return [u for u in out if u]

    def column(self, utype, faction, x, z, heading, count, spacing, name, **kw):
        s, c = math.sin(math.radians(heading)), math.cos(math.radians(heading))
        out = [self.vehicle(utype, faction, x - s * spacing * k, z - c * spacing * k, yaw=heading, name=name, **kw)
               for k in range(count)]
        return [u for u in out if u]

    def fleet(self, ships, faction, cx, cz, heading, name, waypoints=None, hold=False, spacing=900.0):
        """ships: list of types; laid out in a screen: first in the middle, the rest in rings around it."""
        out = []
        for k, t in enumerate(ships):
            if k == 0:
                px, pz = cx, cz
            else:
                ring = 1 + (k - 1) // 6
                a = 2 * math.pi * ((k - 1) % 6) / 6 + math.radians(heading)
                px, pz = cx + ring * spacing * math.sin(a), cz + ring * spacing * math.cos(a)
            out.append(self.ship(t, faction, px, pz, yaw=heading, name=f"{name}_{t}", hold=hold,
                                 waypoints=[dict(w) for w in (waypoints or [])]))
        return [u for u in out if u]

    def furnish_airbase(self, base, faction, hangars=4, shelters=4, helipads=2, bare_ok=False):
        """Hangars, shelters, helipads and a tower for one airbase, attached to it by name.

        Since NOCustomMaps 1.3 (2026-09-19) the loader stands nothing on a custom airfield: "a mission puts its
        own hangars and tower on an airfield ... exactly as on a base-game one". An airbase spawns aircraft only
        from its hangars (seen in headless runs), so without these no player and no AI aircraft can start
        from a land base: the first v0.2 Landfall runs had Boscali's air force vanish once its ships were sunk.
        Layout follows the loader's AirfieldLayout: the apron band runs from the parallel taxiway
        (runway half-width + 140 m) to 120 m inside the levelled rectangle; the rows run parallel to the runway
        with the doors facing it, as in the Swiss Alps missions.
        base: a record from build/<map>/airbases.json."""
        name = base["uniqueName"]
        hdg = base["heading"]
        inner = base.get("runwayWidth", 45.0) / 2 + 140.0
        outer = base["flatHalfAcross"] - 120.0
        if outer - inner < 120.0:
            if bare_ok:
                print(f"  note: {name}: no apron band on the levelled ground; left bare (no aircraft spawn there)")
            else:
                self.problems.append(f"{name}: no apron band on the levelled ground for hangars")
            return []
        along_half = base["flatHalfAlong"]
        s, c = math.sin(math.radians(hdg)), math.cos(math.radians(hdg))
        # The apron side is decided once (tools/apron_side.py, stored in airbases.json) so these buildings stand on
        # the apron the Unity stage paves; older builds without it fall back to the flattest side.
        side = base.get("apronSide")
        if side is None:
            best = None
            for sd in (1, -1):
                bx, bz = c * sd, -s * sd
                pts = [(base["x"] + bx * (inner + 60) + s * t, base["z"] + bz * (inner + 60) + c * t)
                       for t in (-0.3 * along_half, 0.0, 0.3 * along_half)]
                ok = sum(abs(self.g.height(x, z) - base["y"]) < 3.0 for x, z in pts)
                if best is None or ok > best[0]:
                    best = (ok, sd)
            side = best[1]
        ax, az = c * side, -s * side
        face = math.degrees(math.atan2(-ax, -az)) % 360           # doors towards the runway
        out, taken = [], []

        def put(utype, along0, direction, across, label, step):
            """Next free spot along the row from along0 in direction: on flat, dry ground, off every runway
            (Mount Pleasant's second runway crosses the apron) and clear of what this base already has."""
            along = along0
            while abs(along) <= 0.9 * along_half:
                x = base["x"] + ax * across + s * along
                z = base["z"] + az * across + c * along
                if (self.g.ground_problem(x, z, building_clear=0) is None
                        and abs(self.g.height(x, z) - base["y"]) < 4.0
                        and all(math.dist((x, z), q) > 0.8 * step for q in taken)
                        and self.clash(x, z, FOOTPRINT.get(utype, 30.0), kinds=("vehicles",)) is None):
                    taken.append((x, z))
                    out.append(self._unit("buildings", utype, faction, x, self.g.height(x, z), z, face, label, {
                        "capturable": False, "Airbase": name, "placementOffset": 0.0}))
                    return True
                along += direction * step / 2
            return False

        row = inner + 60.0
        heli_row = min(row + 130.0, outer - 30.0)
        tag = name.replace(" ", "")
        placed = {"hangar_med": 0, "shelter1": 0, "Helipad": 0, "controlTower1": 0}
        for utype, n, along0, direction, across, step in (
                ("hangar_med", hangars, 0.15 * along_half, 1, row, 80.0),
                ("shelter1", shelters, -0.15 * along_half, -1, row, 80.0),
                ("Helipad", helipads, 0.15 * along_half, 1, heli_row, 70.0),
                ("controlTower1", 1, -0.15 * along_half, -1, heli_row, 70.0)):
            for k in range(n):
                label = {"hangar_med": "Hangar", "shelter1": "Shelter", "Helipad": "Helipad",
                         "controlTower1": "Tower"}[utype]
                if put(utype, along0, direction, across, f"{tag}_{label}", step):
                    placed[utype] += 1
        if placed["hangar_med"] + placed["shelter1"] == 0:
            if bare_ok:
                print(f"  note: {name}: no room for a single hangar on the apron; left bare (no aircraft spawn there)")
            else:
                self.problems.append(f"{name}: no room for a single hangar on the apron")
        elif placed["hangar_med"] < hangars or placed["shelter1"] < shelters:
            print(f"  note: {name}: {placed['hangar_med']} hangars and {placed['shelter1']} shelters fit "
                  f"(asked {hangars} and {shelters})")
        return out

    def furnish_airbases(self, bases, bare_ok=False):
        """furnish_airbase for every airbase on the map, each for the side that holds it in this mission (the
        mission's airbase override if it has one, else the map's own side). bare_ok: see furnish_airbase."""
        owner = {a["UniqueName"]: a.get("faction") for a in self.m["airbases"]}
        for b in bases:
            self.furnish_airbase(b, owner.get(b["uniqueName"]) or b["faction"], bare_ok=bare_ok)

    @staticmethod
    def waypoint(x, z, objective="Unit Spawn", y=0.0):
        return {"position": {"x": round(x, 2), "y": y, "z": round(z, 2)}, "objective": objective}

    def route(self, name, faction, points, leg_seconds):
        """A multi-point route the game will really drive. The game sends a unit to a waypoint at the moment that
        waypoint's objective STARTS, and of several waypoints sharing one objective it keeps only the first
        (seen in headless runs and in the game's own missions). So each point gets its own hidden WaitSeconds objective
        '<name> Leg<i>'; Leg i lasts leg_seconds[i] (about the travel time of that leg) and then starts Leg i+1.
        Start '<name> Leg0' at the moment the units should set off (from Mission Start's outcome, or in the same
        outcome list right after the SpawnUnit that creates them: spawns are immediate and in order).
        Returns (waypoints, first objective name); give every unit on the route its own copy of the waypoints."""
        if isinstance(leg_seconds, (int, float)):
            leg_seconds = [float(leg_seconds)] * len(points)
        legs = [f"{name} Leg{i}" for i in range(len(points))]
        for i, leg in enumerate(legs):
            outs = [self.start(f"{name}_Go{i + 1}", [legs[i + 1]])] if i + 1 < len(legs) else []
            self.objective("WaitSeconds", leg, faction, hidden=True, outcomes=outs, seconds=float(leg_seconds[i]))
        return [self.waypoint(x, z, leg) for (x, z), leg in zip(points, legs)], legs[0]

    def stop(self, name, objectives):
        return self.outcome("StopOrCompleteObjective", name, options="Stop", objectivesToStart=list(objectives))

    # --- script ---
    def objective(self, otype, name, faction="", display=None, hidden=False, outcomes=(), **fields):
        o = {"Type": otype, "UniqueName": name, "Faction": faction, "DisplayName": display or name, "Hidden": hidden,
             "Outcomes": list(outcomes)}
        o.update(fields)
        self.m["objectives"].append(o)
        return name

    def outcome(self, otype, name, **fields):
        o = {"Type": otype, "UniqueName": name}
        o.update(fields)
        self.m["outcomes"].append(o)
        return name

    def message(self, name, text, faction_only=True):
        return self.outcome("ShowMessage", name, Message=text, PlaySound=True, ObjectiveFactionOnly=faction_only)

    def timed_message(self, faction, seconds, text, tag):
        """A WaitSeconds objective that shows one radio line when it runs out; returns the objective's name."""
        msg = self.message(f"Msg_{tag}", text)
        return self.objective("WaitSeconds", f"Wait_{tag}", faction, hidden=True, outcomes=[msg], seconds=float(seconds))

    def start(self, name, objectives):
        return self.outcome("StartObjective", name, objectivesToStart=list(objectives))

    def spawn(self, name, units):
        return self.outcome("SpawnUnit", name, UnitsToSpawn=[u for u in units if u])

    def end(self, name, end_type, delay=8.0):
        return self.outcome("EndGame", name, endType=end_type, endDelay=delay)

    # --- output ---
    def check(self):
        """Placement problems plus every strict-lint error (game field names, completion rules, references,
        objectives that would complete at once). Warnings are printed but do not block."""
        from mission_lint import lint
        errors, warnings = lint(self.m, [a["uniqueName"] for a in self.g.airbases], strict=True)
        for w in warnings:
            print("lint warning:", w)
        return self.problems + [f"{k} {n} ({t}): {w}" for k, n, t, w in validate(self.m, self.g)] + errors

    def write(self, out_dir, title):
        problems = self.check()
        if problems:
            raise SystemExit("mission not written; fix these first:\n  " + "\n  ".join(problems))
        d = Path(out_dir) / title
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{title}.json").write_text(json.dumps(self.m, indent=4), encoding="utf-8")
        (d / "meta.json").write_text(json.dumps({"FileName": title}), encoding="utf-8")
        return d
