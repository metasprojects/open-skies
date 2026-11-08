"""Trees for an Open Skies map: ESA WorldCover's tree cover, drawn by the base game's own tree renderer.

NOCustomMaps (1.4+) keeps the donor map's TreeRenderer (its mesh, material and GPU culling are base-game assets)
and points it at the map's own positions: the TextAsset the manifest names as "treePositions" (MapFixups.cs,
RetargetDonorTrees and TreeScatterFor, MIT). A map without one has no trees at all.

trees.bytes, exactly as MapFixups.cs reads it:
    no header; per tree three little-endian float32, 12 bytes (TreePositionStride):
        x   map metres east of the map centre
        y   the ground under the tree, metres
        z   map metres north of the map centre
    A file whose length is not a whole number of 12-byte positions is refused (and the map gets no trees).
Swiss Alps ships the same layout: 2,628,724 trees, y 3 to 1,838 m (its ground).

How the forest is made:
  1. WorldCover classes 10 (tree cover) and 95 (mangroves) on a grid of about 32 m (8,192 cells a side on a 261 km
     map), read in strips (textures.Cover, the reader textures.py uses).
  2. Each tree cell gets trees at seeded random spots inside it: 380 per km2 of forest, about the density of the
     game's own Heartland, thinned evenly when the whole forest would pass the budget, map.json "treeMax"
     (default 2,600,000, Swiss Alps' figure).
  3. None in water (ground under 1 m), on a road (centreline + half width + 3 m, roads.bytes + roads_draw.json),
     inside an airfield's levelled ground + 60 m, on a real building (buildings.npz footprint + 2 m), or inside a
     placed town building (cities.bytes, when cities.py ran first).
The same map gives the same trees every time (the random spots are seeded from the map id).

Usage: trees.py <map.json> <worldcover dir> <build dir> [max]
Writes build/trees.bytes and build/trees_preview.png (tree density over the land, 2048 px).
"""
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cities import CATALOGUE, MAGIC, Ground, Roads, airfield_pads  # noqa: E402
from mapgeo import MapGeo  # noqa: E402

TREE_CLASSES = (10, 95)
CELL_TARGET = 32.0          # metres per WorldCover cell, about
PER_KM2 = 380.0             # trees per km2 of forest when the budget allows
ROAD_CLEAR = 3.0
PAD_CLEAR = 60.0
BUILDING_CLEAR = 2.0
MIN_GROUND = 1.0
STRIP = 256


def city_rects(build):
    """Placed town buildings (cities.bytes) as centres, half extents and yaw, or None."""
    path = Path(build) / "cities.bytes"
    if not path.exists():
        return None
    b = path.read_bytes()
    if b[:8] != MAGIC:
        return None
    n = struct.unpack_from("<i", b, 8)[0]
    if n <= 0:
        return None
    rec = np.frombuffer(b, dtype=np.dtype([("t", "u1"), ("pad", "u1", 3), ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                          ("yaw", "<f4")]), count=n, offset=12)
    hw = np.array([CATALOGUE[t][1] / 2.0 for t in rec["t"]])
    hd = np.array([CATALOGUE[t][2] / 2.0 for t in rec["t"]])
    return rec["x"].astype(np.float64), rec["z"].astype(np.float64), hw, hd, np.radians(rec["yaw"].astype(np.float64))


class Clear:
    """Everything a tree must keep off: roads, airfields, real buildings, placed town buildings."""

    def __init__(self, build):
        roads = Roads(build, ROAD_CLEAR)
        self.road_tree = cKDTree(np.column_stack([roads.x, roads.z])) if roads.n else None
        self.road_hw = roads.hw if roads.n else None
        # Samples are at most 5 m apart, so a point beside the centreline is within 2.5 m of one.
        self.road_reach = (float(roads.hw.max()) + 2.5) if roads.n else 0.0
        self.pads = airfield_pads(build, PAD_CLEAR)
        b = Path(build) / "buildings.npz"
        if b.exists():
            d = np.load(b)
            self.bld_tree = cKDTree(np.column_stack([d["x"], d["z"]]))
            self.bld_r = np.sqrt(np.maximum(d["area"], 1.0) / math.pi) + BUILDING_CLEAR
            self.bld_reach = float(self.bld_r.max())
        else:
            self.bld_tree = None
        c = city_rects(build)
        if c is not None:
            self.cx, self.cz, self.chw, self.chd, self.cyaw = c
            self.city_tree = cKDTree(np.column_stack([self.cx, self.cz]))
            self.city_reach = float(np.hypot(self.chw, self.chd).max())
        else:
            self.city_tree = None
        self.counts = {"road": 0, "airfield": 0, "building": 0, "town": 0}

    def keep(self, x, z):
        ok = np.ones(len(x), bool)
        pts = np.column_stack([x, z])
        for r in self.pads:
            dx, dz = x - r.cx, z - r.cz
            inside = (np.abs(dx * r.ux + dz * r.uz) < r.hw) & (np.abs(dx * r.vx + dz * r.vz) < r.hd)
            self.counts["airfield"] += int((inside & ok).sum())
            ok &= ~inside
        if self.road_tree is not None and ok.any():
            idx = np.nonzero(ok)[0]
            dist, near = self.road_tree.query(pts[idx], k=1, distance_upper_bound=self.road_reach)
            hit = np.isfinite(dist)
            hit[hit] = dist[hit] < self.road_hw[near[hit]] + 2.5
            self.counts["road"] += int(hit.sum())
            ok[idx[hit]] = False
        if self.bld_tree is not None and ok.any():
            idx = np.nonzero(ok)[0]
            dist, near = self.bld_tree.query(pts[idx], k=1, distance_upper_bound=self.bld_reach)
            hit = np.isfinite(dist)
            hit[hit] = dist[hit] < self.bld_r[near[hit]]
            self.counts["building"] += int(hit.sum())
            ok[idx[hit]] = False
        if self.city_tree is not None and ok.any():
            idx = np.nonzero(ok)[0]
            dist, near = self.city_tree.query(pts[idx], k=3, distance_upper_bound=self.city_reach)
            hit = np.zeros(len(idx), bool)
            for j in range(near.shape[1]):
                m = np.isfinite(dist[:, j])
                if not m.any():
                    continue
                c = near[m, j]
                dx, dz = x[idx[m]] - self.cx[c], z[idx[m]] - self.cz[c]
                s, co = np.sin(self.cyaw[c]), np.cos(self.cyaw[c])
                u = np.abs(dx * co - dz * s)        # along the building's width (its x axis)
                v = np.abs(dx * s + dz * co)        # along its depth (z axis)
                hit[np.nonzero(m)[0]] |= (u < self.chw[c] + BUILDING_CLEAR) & (v < self.chd[c] + BUILDING_CLEAR)
            self.counts["town"] += int(hit.sum())
            ok[idx[hit]] = False
        return ok


def main(cfg_path, wc_dir, build_dir, max_trees=None):
    t0 = time.time()
    geo = MapGeo.load(cfg_path)
    cfg = geo.cfg
    build = Path(build_dir)
    cap = int(max_trees or cfg.get("treeMax", 2_600_000))
    from textures import Cover
    ground_size = geo.ground_size_m
    size = max(256, int(round(ground_size / CELL_TARGET)))
    px = ground_size / size
    cover = Cover(geo, wc_dir, size)
    if not cover.tifs:
        raise SystemExit(f"no WorldCover tiles in {wc_dir}: run fetch_landcover.py first")

    # Pass 1: which cells are forest (bit-packed: 8 MB for 8,192 x 8,192).
    packed, n_forest = [], 0
    for r0 in range(0, size, STRIP):
        lc = cover.rows(r0, min(r0 + STRIP, size))
        f = np.isin(lc, TREE_CLASSES)
        n_forest += int(f.sum())
        packed.append(np.packbits(f, axis=1))
    cell_km2 = (px / 1000.0) ** 2
    per_cell = min(PER_KM2 * cell_km2, cap / max(n_forest, 1))
    print(f"grid {size} x {size} ({px:.1f} m); forest {n_forest * cell_km2:,.0f} km2 in {n_forest:,} cells; "
          f"{per_cell / cell_km2:.0f} trees per km2 (budget {cap:,}); {time.time() - t0:.0f} s")

    # Pass 2: spots, then everything that is in the way taken out.
    ground = Ground(build, cfg)
    clear = Clear(build)
    seed = int.from_bytes(cfg["mapId"].encode()[:8].ljust(8, b"\0"), "little")
    rng = np.random.default_rng(seed)
    half_g = ground_size / 2
    scale = geo.scale
    out, water = [], 0
    base, frac = int(math.floor(per_cell)), per_cell - math.floor(per_cell)
    for k, r0 in enumerate(range(0, size, STRIP)):
        f = np.unpackbits(packed[k], axis=1, count=size).astype(bool)
        rr, cc = np.nonzero(f)
        if not len(rr):
            continue
        n_here = base + (rng.random(len(rr)) < frac)
        rr, cc = np.repeat(rr, n_here), np.repeat(cc, n_here)
        if not len(rr):
            continue
        xg = -half_g + (cc + rng.random(len(cc))) * px
        zg = half_g - (r0 + rr + rng.random(len(rr))) * px
        x, z = xg / scale, zg / scale
        y = ground.at(x, z)
        dry = y >= MIN_GROUND
        water += int((~dry).sum())
        x, z, y = x[dry], z[dry], y[dry]
        keep = clear.keep(x, z)
        out.append(np.column_stack([x[keep], y[keep], z[keep]]).astype("<f4"))
    trees = np.concatenate(out) if out else np.zeros((0, 3), "<f4")
    if len(trees) > cap:
        trees = trees[np.sort(rng.choice(len(trees), cap, replace=False))]
    path = build / "trees.bytes"
    trees.tofile(path)
    counts = dict(clear.counts, water=water)
    print(f"{len(trees):,} trees -> {path} ({path.stat().st_size / 1e6:.1f} MB); kept off: {counts}; "
          f"{time.time() - t0:.0f} s")
    check(path, geo.size_m / 2)
    preview(trees, ground, build, geo.size_m / 2)


def check(path, half):
    """Read the file back the way the plugin does."""
    n = path.stat().st_size
    assert n % 12 == 0, "not a whole number of 12-byte positions"
    t = np.fromfile(path, "<f4").reshape(-1, 3)
    if len(t):
        assert np.all(np.isfinite(t)), "not finite"
        assert np.all(np.abs(t[:, 0]) < half) and np.all(np.abs(t[:, 2]) < half), "outside the map"
        assert t[:, 1].min() >= MIN_GROUND - 0.01, "in water"
    print(f"check: {len(t):,} positions read back OK")


def preview(trees, ground, build, half, px=2048):
    from PIL import Image
    step = max(1, ground.n // px)
    land = np.asarray(ground.h[::step, ::step]) > 0
    land = np.array(Image.fromarray(land.astype(np.uint8) * 255).resize((px, px))) > 0
    if len(trees):
        dens, _, _ = np.histogram2d(half - trees[:, 2], trees[:, 0] + half, bins=px, range=[[0, 2 * half], [0, 2 * half]])
        dens = np.clip(dens / max(np.percentile(dens[dens > 0], 95), 1), 0, 1)
    else:
        dens = np.zeros((px, px))
    rgb = np.zeros((px, px, 3), np.float32)
    rgb[land] = (52, 56, 50)
    rgb[~land] = (16, 22, 32)
    green = np.array([70, 150, 70], np.float32)
    rgb = rgb * (1 - dens[..., None]) + green * dens[..., None]
    Image.fromarray(rgb.astype(np.uint8)).save(build / "trees_preview.png")
    print("preview written")


if __name__ == "__main__":
    main(*sys.argv[1:])
