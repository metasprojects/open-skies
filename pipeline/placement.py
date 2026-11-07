"""Placement checker: where a unit may stand on a NO-Earth map, and a validator for whole missions.

Game frame: x east, z north, metres from the map centre; y up. Uses the build folder's heights.npy
(row 0 = north edge), buildings.npz (real building footprints) and airbases.json (levelled pads, runways).

Rules (see docs/MISSION-PLAYBOOK.md section 7):
  ground units and buildings: on dry land, slope under MAX_SLOPE, clear of real buildings, off runways
  ships: water at least DRAFT[type] + KEEL_CLEARANCE deep, clear of the shore by SHORE_CLEAR
  aircraft: on an airbase pad (parked) or at least 150 m above the ground (airborne)
"""
import json
import math
from pathlib import Path

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

MAX_SLOPE = 12.0
BUILDING_CLEAR = 15.0
KEEL_CLEARANCE = 4.0
SHORE_CLEAR = 150.0
DRAFT = {"FleetCarrier1": 12, "AssaultCarrier1": 9, "SmallCarrier1": 8, "Destroyer1": 7, "Frigate1": 6, "Corvette1": 5,
         "PatrolBoat1": 2, "LandingCraft1": 1.5}


def yaw_quat(yaw_deg):
    """Rotation about +y by yaw (0 = facing north / +z, 90 = east), as the game's quaternion dict."""
    h = math.radians(yaw_deg) / 2
    return {"x": 0.0, "y": math.sin(h), "z": 0.0, "w": math.cos(h)}


class MapGround:
    def __init__(self, build_dir, cfg):
        b = Path(build_dir)
        self.h = np.load(b / "heights.npy", mmap_mode="r")
        self.n = self.h.shape[0]
        self.size = cfg["tileMetres"] * cfg["tilesPerSide"]
        self.half = self.size / 2
        self.pitch = self.size / (self.n - 1)
        self.land = None
        bz = b / "buildings.npz"
        if bz.exists():
            d = np.load(bz, allow_pickle=True)
            self.bxz = np.column_stack([d["x"], d["z"]])
            self.brad = np.sqrt(np.maximum(d["area"], 1) / np.pi)
            self.btree = cKDTree(self.bxz)
        else:
            self.btree = None
        ab = b / "airbases.json"
        self.airbases = json.loads(ab.read_text(encoding="utf-8")) if ab.exists() else []
        self._dist_land = None

    # --- terrain ---
    def height(self, x, z):
        """Ground height exactly as the game's terrain collider has it.

        The game puts units at the height written in the mission and does not snap them to the ground
        (units appear exactly at globalPosition, seen in headless runs), so this must match the mesh, not a
        smooth average. MapBuilder.TileMesh splits each 51.2 m cell along the diagonal from (a+1, b) to (a, b+1)
        (a east, b north): triangles (a,b)-(a,b+1)-(a+1,b) and (a+1,b)-(a,b+1)-(a+1,b+1)."""
        n = self.n
        gx = min(max((x + self.half) / self.pitch, 0.0), n - 1.000001)
        gz = min(max((z + self.half) / self.pitch, 0.0), n - 1.000001)
        a, b = int(gx), int(gz)
        fx, fz = gx - a, gz - b

        def hv(i, j):  # i east index, j north index
            return float(self.h[(n - 1) - j, i])

        if fx + fz <= 1.0:
            h00, h10, h01 = hv(a, b), hv(a + 1, b), hv(a, b + 1)
            return h00 + fx * (h10 - h00) + fz * (h01 - h00)
        h11, h01, h10 = hv(a + 1, b + 1), hv(a, b + 1), hv(a + 1, b)
        return h11 + (1 - fx) * (h01 - h11) + (1 - fz) * (h10 - h11)

    def slope(self, x, z, d=None):
        d = d or self.pitch
        dx = (self.height(x + d, z) - self.height(x - d, z)) / (2 * d)
        dz = (self.height(x, z + d) - self.height(x, z - d)) / (2 * d)
        return math.degrees(math.atan(math.hypot(dx, dz)))

    def shore_distance(self, x, z):
        """Metres from (x, z) to the nearest dry land (0 on land)."""
        if self._dist_land is None:
            step = 4
            land = np.asarray(self.h[::step, ::step]) > 0
            self._dist_land = ndimage.distance_transform_edt(~land) * self.pitch * step
            self._dl_step = step
        r = int(round((self.half - z) / (self.pitch * self._dl_step)))
        c = int(round((x + self.half) / (self.pitch * self._dl_step)))
        r = min(max(r, 0), self._dist_land.shape[0] - 1)
        c = min(max(c, 0), self._dist_land.shape[1] - 1)
        return float(self._dist_land[r, c])

    def inside(self, x, z, margin=500.0):
        return abs(x) < self.half - margin and abs(z) < self.half - margin

    # --- features ---
    def near_building(self, x, z, clear=BUILDING_CLEAR):
        if self.btree is None:
            return False
        idx = self.btree.query_ball_point([x, z], r=clear + 80)
        return any(math.dist((x, z), self.bxz[i]) < self.brad[i] + clear for i in idx)

    def on_runway(self, x, z, margin=60.0):
        for a in self.airbases:
            runs = [((a["x"], a["z"]), a["heading"], a["runwayLength"], a["runwayWidth"])]
            for e in a.get("extraRunways", []):
                cx, cz = (e["startX"] + e["endX"]) / 2, (e["startZ"] + e["endZ"]) / 2
                hdg = math.degrees(math.atan2(e["endX"] - e["startX"], e["endZ"] - e["startZ"]))
                runs.append(((cx, cz), hdg, math.dist((e["startX"], e["startZ"]), (e["endX"], e["endZ"])), e["width"]))
            for (cx, cz), hdg, length, width in runs:
                s, c = math.sin(math.radians(hdg)), math.cos(math.radians(hdg))
                along = (x - cx) * s + (z - cz) * c
                across = (x - cx) * c - (z - cz) * s
                if abs(along) <= length / 2 + margin and abs(across) <= width / 2 + margin:
                    return True
        return False

    # --- rules ---
    def ground_problem(self, x, z, max_slope=MAX_SLOPE, building_clear=BUILDING_CLEAR):
        if not self.inside(x, z):
            return "outside the map"
        if self.height(x, z) <= 0.5:
            return "in water"
        if self.slope(x, z) > max_slope:
            return f"slope {self.slope(x, z):.0f} deg"
        if building_clear and self.near_building(x, z, building_clear):
            return "inside or against a building"
        if self.on_runway(x, z):
            return "on a runway"
        return None

    def sea_problem(self, x, z, unit_type):
        if not self.inside(x, z, 2000):
            return "outside the map"
        depth = -self.height(x, z)
        need = DRAFT.get(unit_type, 6) + KEEL_CLEARANCE
        if depth < need:
            return f"water {depth:.0f} m deep, needs {need:.0f} m"
        if unit_type != "LandingCraft1" and self.shore_distance(x, z) < SHORE_CLEAR:
            return "too close to shore"
        return None

    def find(self, x, z, ok, radius=3000.0, step=40.0):
        """Nearest point to (x, z) where ok(x, z) is None, searching outward in rings; None if none within radius."""
        if ok(x, z) is None:
            return x, z
        r = step
        while r <= radius:
            n = max(8, int(2 * math.pi * r / step))
            for k in range(n):
                a = 2 * math.pi * k / n
                px, pz = x + r * math.sin(a), z + r * math.cos(a)
                if ok(px, pz) is None:
                    return px, pz
            r += step
        return None

    def find_ground(self, x, z, radius=3000.0, **kw):
        return self.find(x, z, lambda a, b: self.ground_problem(a, b, **kw), radius)

    def find_sea(self, x, z, unit_type, radius=8000.0):
        return self.find(x, z, lambda a, b: self.sea_problem(a, b, unit_type), radius, step=150.0)


def validate(mission, ground):
    """Every problem in a mission's unit placement, as (list, UniqueName, type, problem)."""
    problems = []
    for key in ("vehicles", "buildings", "containers"):
        for u in mission.get(key, []) or []:
            p = u["globalPosition"]
            why = ground.ground_problem(p["x"], p["z"], building_clear=0 if key != "vehicles" else BUILDING_CLEAR)
            if why is None and not (-0.05 <= p["y"] - ground.height(p["x"], p["z"]) <= 1.5):
                why = f"height {p['y']:.0f} m but ground is {ground.height(p['x'], p['z']):.0f} m"
            if why:
                problems.append((key, u["UniqueName"], u["type"], why))
    for u in mission.get("ships", []) or []:
        p = u["globalPosition"]
        why = ground.sea_problem(p["x"], p["z"], u["type"])
        if why:
            problems.append(("ships", u["UniqueName"], u["type"], why))
    for u in mission.get("aircraft", []) or []:
        p = u["globalPosition"]
        g = ground.height(p["x"], p["z"])
        parked = (u.get("startingSpeed") or 0) <= 1
        if parked and p["y"] - max(g, 0) > 5:
            problems.append(("aircraft", u["UniqueName"], u["type"], "parked but floating"))
        if not parked and p["y"] - max(g, 0) < 150:
            problems.append(("aircraft", u["UniqueName"], u["type"], "airborne under 150 m"))
    return problems
