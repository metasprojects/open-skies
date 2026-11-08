"""Towns for an Open Skies map: the real building footprints, stood up as the base game's own buildings.

NOCustomMaps (1.4+) builds a map's towns at load from a table of placements: a bundle cannot ship buildings, only
where they go. The plugin clones the base game's Ignus buildings (CityBuilder.cs, Core/CityData.cs and
Core/CityCatalogue.cs in NOCustomMaps, MIT): 18 low-rise terraces (suburbs_block*, 7-12 m tall, 25-163 m long) and
8 commercial blocks (commercial_block*, 27-31 m). The manifest names the table as "cityPlacements".

cities.bytes, little-endian, exactly as Core/CityData.cs reads it:
    8 bytes    magic b"NOCITY01"
    int32      count (the plugin refuses more than 2,000,000)
    count x 20 bytes:
        uint8      type: index into CityCatalogue.Entries (CATALOGUE below, same order)
        3 bytes    zero padding, so the floats are 4-byte aligned
        float32 X  the middle of the building's footprint, map metres east of the map centre
        float32 Y  the ground the building stands on (its base), metres
        float32 Z  the middle of the footprint, map metres north of the map centre
        float32 Yaw degrees about the vertical: 0 puts the type's Depth along north (+z), 90 along east
The plugin takes each donor's pivot offset out itself, so X/Z really are the footprint's middle.

How the towns are made (build/buildings.npz from buildings.py is the source):
  1. Real buildings with a long side of 28 m or more keep their own place: the catalogue type closest to their own
     footprint, turned to their own heading. They become commercial blocks (27-31 m) when they are tall (18 m+), when
     they are shops or civic buildings in a town centre (50 m cell 20 %+ built up), or industrial and 15 m+; a low
     shed or warehouse stays low-rise, so a harbour town does not grow office towers.
  2. Built-up land is mapped on a 50 m grid (share of each cell covered by real footprints). Cells over 8 % are
     filled with terraces, densest first, each turned to the local street grain (the main heading of the 12
     nearest real buildings) and centred on the real buildings in the cell. When the type the mix picks does not
     fit, smaller ones are tried (the catalogue is sorted by long side within each tier for exactly that). A cell
     is commercial when most of its built area is tall or shops/civic and it is 20 %+ built up.
  3. Nothing goes in water, on ground too steep for it (the base would be buried more than 4 m on the uphill
     side), on a road (roads.bytes centrelines + roads_draw.json widths + 4 m), inside an airfield's levelled ground
     + 150 m (where the runway, taxiways and aprons go), within 9 km of the map edge, or over another building.
  4. At most map.json "cityMax" placements (default 60,000; Swiss Alps ships 65,137): at most half of them for
     step 1, the rest for step 2, both densest first, so the town centres are always there.
The base Y is the lowest ground under the footprint, so no corner floats.

Usage: cities.py <map.json> <build dir> [max]
Writes build/cities.bytes and build/cities_preview.png (whole map) + cities_preview_main.png (the main airbase).
"""
import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapgeo import MapGeo  # noqa: E402

# Core/CityCatalogue.cs, NOCustomMaps 1.4.2, in the same order: (donor name, width (x), depth (z), height, commercial).
CATALOGUE = [
    ("suburbs_block19", 44, 38, 7, 0), ("suburbs_block23", 31, 52, 8, 0), ("suburbs_block13", 31, 91, 9, 0),
    ("suburbs_block14", 42, 93, 10, 0), ("suburbs_block17", 99, 39, 10, 0), ("suburbs_block7", 108, 81, 10, 0),
    ("suburbs_block6", 113, 39, 12, 0), ("suburbs_block1", 114, 25, 9, 0), ("suburbs_block5", 116, 24, 9, 0),
    ("suburbs_block9", 30, 121, 7, 0), ("suburbs_block10", 31, 121, 10, 0), ("suburbs_block11", 32, 123, 9, 0),
    ("suburbs_block3", 123, 37, 10, 0), ("suburbs_block8", 123, 39, 10, 0), ("suburbs_block2", 35, 134, 10, 0),
    ("suburbs_block4", 35, 135, 10, 0), ("suburbs_block15", 162, 32, 9, 0), ("suburbs_block16", 163, 36, 9, 0),
    ("commercial_block3", 70, 53, 31, 1), ("commercial_block13", 74, 36, 31, 1), ("commercial_block14", 80, 63, 30, 1),
    ("commercial_block10", 44, 105, 27, 1), ("commercial_block16", 83, 105, 28, 1), ("commercial_block2", 49, 106, 27, 1),
    ("commercial_block15", 50, 107, 31, 1), ("commercial_block1", 112, 106, 31, 1),
]
LOW = [i for i, c in enumerate(CATALOGUE) if not c[4]]
COM = [i for i, c in enumerate(CATALOGUE) if c[4]]
# How often Swiss Alps uses each type: a mix that reads as real towns from the air.
MIX = {0: 7124, 1: 22323, 2: 6396, 3: 3191, 4: 2663, 5: 402, 6: 880, 7: 1512, 8: 4579, 9: 1337, 10: 1404, 11: 1562,
       12: 863, 13: 1073, 14: 796, 15: 1061, 16: 676, 17: 515, 18: 705, 19: 3435, 20: 779, 21: 792, 22: 154, 23: 318,
       24: 511, 25: 86}
MAGIC = b"NOCITY01"

CELL = 50.0                 # built-up grid
FILL_SHARE = 0.08           # a cell this covered by real footprints gets town
BIG_LONG = 28.0             # a real building this long keeps its own place
TALL = 18.0
ROAD_CLEAR = 4.0
PAD_CLEAR = 150.0
GAP = 4.0                   # between two placed buildings
EDGE_M = 9000.0
MAX_BURY = 4.0              # metres of the base under ground on the uphill side
CENTRE_ZONES = (1, 6)           # zoning.py: commercial, civic
INDUSTRIAL = 2
CENTRE_SHARE = 0.20             # a cell this built up counts as a town centre


class Ground:
    """The terrain exactly as the game's collider has it (placement.MapGround.height, vectorised)."""

    def __init__(self, build, cfg):
        self.h = np.load(Path(build) / "heights.npy", mmap_mode="r")
        self.n = self.h.shape[0]
        size = cfg["tileMetres"] * cfg["tilesPerSide"]
        self.half = size / 2
        self.pitch = size / (self.n - 1)

    def at(self, x, z):
        n = self.n
        gx = np.clip((np.asarray(x, np.float64) + self.half) / self.pitch, 0.0, n - 1.000001)
        gz = np.clip((np.asarray(z, np.float64) + self.half) / self.pitch, 0.0, n - 1.000001)
        a, b = gx.astype(np.int64), gz.astype(np.int64)
        fx, fz = gx - a, gz - b
        h = self.h

        def hv(i, j):
            return np.asarray(h[(n - 1) - j, i], np.float64)

        h00, h10, h01, h11 = hv(a, b), hv(a + 1, b), hv(a, b + 1), hv(a + 1, b + 1)
        lower = h00 + fx * (h10 - h00) + fz * (h01 - h00)
        upper = h11 + (1 - fx) * (h01 - h11) + (1 - fz) * (h10 - h11)
        return np.where(fx + fz <= 1.0, lower, upper)


class Rect:
    """An oriented rectangle: centre, half extents along its own x (width) and z (depth), yaw in degrees."""
    __slots__ = ("cx", "cz", "hw", "hd", "ux", "uz", "vx", "vz")

    def __init__(self, cx, cz, hw, hd, yaw):
        r = math.radians(yaw)
        s, c = math.sin(r), math.cos(r)
        self.cx, self.cz, self.hw, self.hd = cx, cz, hw, hd
        self.ux, self.uz = c, -s          # the rectangle's x axis (width) in map x/z
        self.vx, self.vz = s, c           # its z axis (depth): yaw 0 = north

    def corners(self):
        out = []
        for a in (-1, 1):
            for b in (-1, 1):
                out.append((self.cx + a * self.hw * self.ux + b * self.hd * self.vx,
                            self.cz + a * self.hw * self.uz + b * self.hd * self.vz))
        return out

    def radius(self):
        return math.hypot(self.hw, self.hd)

    def overlaps(self, o, gap=0.0):
        dx, dz = o.cx - self.cx, o.cz - self.cz
        for ax, az in ((self.ux, self.uz), (self.vx, self.vz), (o.ux, o.uz), (o.vx, o.vz)):
            ra = self.hw * abs(self.ux * ax + self.uz * az) + self.hd * abs(self.vx * ax + self.vz * az)
            rb = o.hw * abs(o.ux * ax + o.uz * az) + o.hd * abs(o.vx * ax + o.vz * az)
            if abs(dx * ax + dz * az) > ra + rb + gap:
                return False
        return True


class Grid:
    """Rectangles bucketed by 128 m cells, for overlap tests."""

    def __init__(self, cell=128.0):
        self.cell, self.cells = cell, {}

    def _keys(self, r, pad):
        e = r.radius() + pad
        c = self.cell
        for i in range(int(math.floor((r.cx - e) / c)), int(math.floor((r.cx + e) / c)) + 1):
            for j in range(int(math.floor((r.cz - e) / c)), int(math.floor((r.cz + e) / c)) + 1):
                yield i, j

    def hits(self, r, gap):
        seen = set()
        for k in self._keys(r, gap + 170.0):
            for o in self.cells.get(k, ()):
                if id(o) not in seen:
                    seen.add(id(o))
                    if r.overlaps(o, gap):
                        return True
        return False

    def add(self, r):
        for k in self._keys(r, 0.0):
            self.cells.setdefault(k, []).append(r)


class Roads:
    """Road centreline samples (every 5 m or closer) with the half width each must be kept clear of."""

    def __init__(self, build, clear):
        self.cell = 64.0
        path = Path(build) / "roads.bytes"
        if not path.exists():
            self.n = 0
            return
        from roads import read_roads
        recs = read_roads(path)
        widths = None
        draw = Path(build) / "roads_draw.json"
        if draw.exists():
            d = json.loads(draw.read_text(encoding="utf-8"))
            if len(d.get("width", [])) == len(recs):
                widths, kinds = d["width"], d.get("kind", [0] * len(recs))
        by_class = (14.0, 10.0, 8.0, 6.0)                   # the road widths roads.py falls back to
        xs, zs, hw = [], [], []
        for i, r in enumerate(recs):
            if widths is not None and kinds[i] == 1:        # tunnels: nothing on the surface
                continue
            p = np.asarray(r["pts"], np.float64)
            if len(p) < 2:
                continue
            w = widths[i] if widths is not None else by_class[min(r["cls"], 3)]
            seg = np.hypot(np.diff(p[:, 0]), np.diff(p[:, 2]))
            s = np.concatenate([[0.0], np.cumsum(seg)])
            t = np.linspace(0.0, s[-1], max(2, int(math.ceil(s[-1] / 5.0)) + 1))
            xs.append(np.interp(t, s, p[:, 0]))
            zs.append(np.interp(t, s, p[:, 2]))
            hw.append(np.full(len(t), w / 2.0 + clear, np.float32))
        if not xs:
            self.n = 0
            return
        x, z, h = np.concatenate(xs).astype(np.float32), np.concatenate(zs).astype(np.float32), np.concatenate(hw)
        key = self._key(x, z)
        order = np.argsort(key, kind="stable")
        self.x, self.z, self.hw, self.key = x[order], z[order], h[order], key[order]
        self.n = len(self.x)
        self.max_hw = float(self.hw.max())

    def _key(self, x, z):
        return (np.floor(np.asarray(x) / self.cell).astype(np.int64) + 100000) * 1000003 + \
               (np.floor(np.asarray(z) / self.cell).astype(np.int64) + 100000)

    def near(self, x0, z0, x1, z1):
        """Indices of samples in the cells covering a box."""
        out = []
        for i in range(int(math.floor(x0 / self.cell)), int(math.floor(x1 / self.cell)) + 1):
            k0 = (i + 100000) * 1000003 + int(math.floor(z0 / self.cell)) + 100000
            k1 = (i + 100000) * 1000003 + int(math.floor(z1 / self.cell)) + 100000
            a, b = np.searchsorted(self.key, k0, "left"), np.searchsorted(self.key, k1, "right")
            if b > a:
                out.append(np.arange(a, b))
        return np.concatenate(out) if out else np.empty(0, np.int64)

    def hits(self, r):
        if not self.n:
            return False
        e = r.radius() + self.max_hw
        idx = self.near(r.cx - e, r.cz - e, r.cx + e, r.cz + e)
        if not len(idx):
            return False
        dx, dz = self.x[idx] - r.cx, self.z[idx] - r.cz
        u = np.abs(dx * r.ux + dz * r.uz)
        v = np.abs(dx * r.vx + dz * r.vz)
        hw = self.hw[idx]
        return bool(np.any((u < r.hw + hw) & (v < r.hd + hw)))


def airfield_pads(build, clear):
    """Each airbase's levelled rectangle (airbases.py), grown by `clear`, as Rects."""
    path = Path(build) / "airbases.json"
    if not path.exists():
        return []
    out = []
    for a in json.loads(path.read_text(encoding="utf-8")):
        out.append(Rect(a["x"], a["z"], a["flatHalfAcross"] + clear, a["flatHalfAlong"] + clear, a["heading"]))
    return out


def fits(rect, ground, roads, pads, placed, half, edge):
    """None if a building can stand on this rectangle, else the reason it cannot. Also returns the base height."""
    if max(abs(rect.cx), abs(rect.cz)) + rect.radius() > half - edge:
        return "edge", 0.0
    if any(rect.overlaps(p) for p in pads):
        return "airfield", 0.0
    # A 3 x 3 grid over the footprint: a 163 m terrace can straddle a ridge its corners miss.
    a = np.array([-1.0, 0.0, 1.0])
    A, B = np.meshgrid(a, a)
    A, B = A.ravel() * rect.hw, B.ravel() * rect.hd
    g = ground.at(rect.cx + A * rect.ux + B * rect.vx, rect.cz + A * rect.uz + B * rect.vz)
    if g.min() <= 0.6:
        return "water", 0.0
    if g.max() - g.min() > MAX_BURY:
        return "slope", 0.0
    if placed.hits(rect, GAP):
        return "crowded", 0.0
    if roads.hits(rect):
        return "road", 0.0
    return None, float(g.min())


def type_yaw(t, bearing):
    """Yaw that lays type t's long side along a footprint whose long side points to `bearing` (degrees)."""
    _, w, d, _, _ = CATALOGUE[t]
    return (bearing if d >= w else bearing - 90.0) % 360.0


def closest_type(long_, short_, candidates):
    best, cost = None, None
    for t in candidates:
        _, w, d, _, _ = CATALOGUE[t]
        tl, ts = max(w, d), min(w, d)
        if tl > long_ * 1.25 + 10 or ts > short_ * 1.4 + 8:
            continue
        c = abs(tl - long_) + abs(ts - short_)
        if cost is None or c < cost:
            best, cost = t, c
    return best


def main(cfg_path, build_dir, max_placements=None):
    t0 = time.time()
    geo = MapGeo.load(cfg_path)
    cfg = geo.cfg
    build = Path(build_dir)
    cap = int(max_placements or cfg.get("cityMax", 60000))
    half = geo.size_m / 2
    src = build / "buildings.npz"
    if not src.exists():
        raise SystemExit(f"no {src}: run buildings.py first")
    d = np.load(src)
    x, z = d["x"].astype(np.float64), d["z"].astype(np.float64)
    area, long_, short_ = d["area"], d["long"], d["short"]
    yaw, height, zone = d["yaw"], d["height"], d["zone"]
    n = len(x)
    print(f"{n:,} real buildings; cap {cap:,} placements")
    ground = Ground(build, cfg)
    roads = Roads(build, ROAD_CLEAR)
    pads = airfield_pads(build, PAD_CLEAR)
    print(f"{roads.n:,} road samples, {len(pads)} airfields kept clear")
    placed = Grid()
    out, why = [], {}
    rng = np.random.default_rng(int.from_bytes(cfg["mapId"].encode()[:8].ljust(8, b"\0"), "little"))

    def put(t, cx, cz, yaw_):
        _, w, dd, _, _ = CATALOGUE[t]
        r = Rect(cx, cz, w / 2.0, dd / 2.0, yaw_)
        reason, base = fits(r, ground, roads, pads, placed, half, EDGE_M)
        if reason:
            why[reason] = why.get(reason, 0) + 1
            return False
        placed.add(r)
        out.append((t, cx, base, cz, yaw_))
        return True

    # 2. Built-up land on a 50 m grid, densest cells first.
    ncell = int(math.ceil(geo.size_m / CELL))
    ix = np.clip(((x + half) / CELL).astype(np.int64), 0, ncell - 1)
    iz = np.clip(((z + half) / CELL).astype(np.int64), 0, ncell - 1)
    flat = iz * ncell + ix
    cells, inv = np.unique(flat, return_inverse=True)
    cov = np.bincount(inv, weights=area) / (CELL * CELL)
    cnt = np.bincount(inv)
    cxm = np.bincount(inv, weights=area * x) / np.maximum(np.bincount(inv, weights=area), 1e-6)
    czm = np.bincount(inv, weights=area * z) / np.maximum(np.bincount(inv, weights=area), 1e-6)
    com_area = np.bincount(inv, weights=area * np.isin(zone, CENTRE_ZONES))
    tall_area = np.bincount(inv, weights=area * (height >= TALL))
    # 1. Big real buildings keep their own place, size and heading. Largest first.
    big = np.nonzero((long_ >= BIG_LONG) & (short_ >= 12.0))[0]
    big = big[np.argsort(-area[big], kind="stable")]
    big_cap = cap // 2
    n_big = 0
    for i in big:
        if n_big >= big_cap:
            break
        centre = cov[inv[i]] >= CENTRE_SHARE
        com = height[i] >= TALL or (zone[i] in CENTRE_ZONES and centre) or (zone[i] == INDUSTRIAL and height[i] >= 15)
        t = closest_type(float(long_[i]), float(short_[i]), COM if com else LOW)
        if t is None and com:
            t = closest_type(float(long_[i]), float(short_[i]), LOW)
        if t is None:
            continue
        flip = 180.0 if rng.random() < 0.5 else 0.0
        if put(t, x[i], z[i], (type_yaw(t, float(yaw[i])) + flip) % 360.0):
            n_big += 1
    print(f"step 1: {n_big:,} big real buildings placed ({len(big):,} candidates)")

    # 2. Fill the built-up cells, densest first.
    sites = np.nonzero((cov >= FILL_SHARE) & (cnt >= 2))[0]
    sites = sites[np.argsort(-cov[sites], kind="stable")]
    print(f"step 2: {len(sites):,} built-up cells over {FILL_SHARE:.0%} covered")
    # The street grain: main heading (mod 90) of the 12 nearest real buildings, by footprint.
    from scipy.spatial import cKDTree
    tree = cKDTree(np.column_stack([x, z]))
    k = min(12, n)
    grain = np.zeros(len(sites))
    for a in range(0, len(sites), 50000):
        part = sites[a:a + 50000]
        _, nb = tree.query(np.column_stack([cxm[part], czm[part]]), k=k)
        nb = nb.reshape(len(part), -1)
        ang = np.radians(yaw[nb].astype(np.float64)) * 4.0
        w = area[nb]
        grain[a:a + len(part)] = np.degrees(np.arctan2((np.sin(ang) * w).sum(1), (np.cos(ang) * w).sum(1))) / 4.0
    del tree
    mix_low = np.array([MIX[t] for t in LOW], np.float64)
    mix_low /= mix_low.sum()
    mix_com = np.array([MIX[t] for t in COM], np.float64)
    mix_com /= mix_com.sum()
    n_fill = 0
    for s, g in zip(sites, grain):
        if len(out) >= cap:
            break
        com = (com_area[s] + tall_area[s]) > 0.5 * cov[s] * CELL * CELL and cov[s] >= CENTRE_SHARE
        pool = COM if com else LOW
        first = int(rng.choice(len(pool), p=mix_com if com else mix_low))
        tries = [pool[first]] + [pool[j] for j in range(first - 1, -1, -1)][:3]
        if com:
            tries += LOW[:2]
        bearing = g + (90.0 if rng.random() < 0.5 else 0.0)
        flip = 180.0 if rng.random() < 0.5 else 0.0
        for t in tries:
            if put(t, float(cxm[s]), float(czm[s]), (type_yaw(t, bearing) + flip) % 360.0):
                n_fill += 1
                break
    print(f"step 2: {n_fill:,} town blocks placed")

    with open(build / "cities.bytes", "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<i", len(out)))
        for t, px, py, pz, pyaw in out:
            f.write(struct.pack("<B3xffff", t, px, py, pz, pyaw))
    types = np.bincount([o[0] for o in out], minlength=len(CATALOGUE)) if out else np.zeros(len(CATALOGUE), int)
    print(f"{len(out):,} placements ({int(types[COM].sum()):,} commercial, {int(types[LOW].sum()):,} low-rise) "
          f"-> {build / 'cities.bytes'} ({(build / 'cities.bytes').stat().st_size / 1e6:.1f} MB); "
          f"not placed: {json.dumps(why)}; {time.time() - t0:.0f} s")
    check(build / "cities.bytes", half)
    preview(out, ground, build, half, cfg)


def check(path, half):
    """Read the file back the way the plugin does and refuse anything it would."""
    b = path.read_bytes()
    assert b[:8] == MAGIC, "magic"
    count = struct.unpack_from("<i", b, 8)[0]
    assert 0 <= count <= 2_000_000 and len(b) == 12 + 20 * count, "length"
    if count:
        rec = np.frombuffer(b, dtype=np.dtype([("t", "u1"), ("pad", "u1", 3), ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                              ("yaw", "<f4")]), count=count, offset=12)
        assert rec["t"].max() < len(CATALOGUE) and not rec["pad"].any(), "type/padding"
        assert np.all(np.abs(rec["x"]) < half) and np.all(np.abs(rec["z"]) < half), "outside the map"
        assert np.all(np.isfinite(rec["y"])) and np.all(np.isfinite(rec["yaw"])), "not finite"
    print(f"check: {count:,} placements read back OK")


def preview(out, ground, build, half, cfg):
    """Whole map (4096 px) and a 12 km close-up around the first airbase: footprints in bone (low-rise) and red
    (commercial) over the land."""
    from PIL import Image, ImageDraw
    step = max(1, ground.n // 2048)
    land = np.asarray(ground.h[::step, ::step]) > 0
    for name, (x0, z0, span, px) in views(build, half):
        img = Image.new("RGB", (px, px), (16, 18, 22))
        if span >= 2 * half - 1:
            img.paste(Image.fromarray((land * 46 + 16).astype(np.uint8)).convert("RGB").resize((px, px)))
        else:
            n = 512
            gx = np.linspace(x0, x0 + span, n)
            gz = np.linspace(z0, z0 - span, n)
            X, Z = np.meshgrid(gx, gz)
            hh = ground.at(X.ravel(), Z.ravel()).reshape(n, n)
            img.paste(Image.fromarray(((hh > 0) * 46 + 16).astype(np.uint8)).convert("RGB").resize((px, px)))
        dr = ImageDraw.Draw(img)
        k = px / span
        for t, cx, _, cz, yaw_ in out:
            if not (x0 - 200 <= cx <= x0 + span + 200 and z0 - span - 200 <= cz <= z0 + 200):
                continue
            _, w, d, _, com = CATALOGUE[t]
            pts = [((px_ - x0) * k, (z0 - pz_) * k) for px_, pz_ in Rect(cx, cz, w / 2, d / 2, yaw_).corners()]
            pts = [pts[0], pts[1], pts[3], pts[2]]
            dr.polygon(pts, fill=(255, 69, 56) if com else (237, 235, 230))
        img.save(build / name)
    print("previews written")


def views(build, half):
    yield "cities_preview.png", (-half, half, 2 * half, 4096)
    path = Path(build) / "airbases.json"
    if path.exists():
        bases = json.loads(path.read_text(encoding="utf-8"))
        if bases:
            b = max(bases, key=lambda a: a.get("runwayLength", 0))
            yield "cities_preview_main.png", (b["x"] - 6000, b["z"] + 6000, 12000, 2048)


if __name__ == "__main__":
    main(*sys.argv[1:])
