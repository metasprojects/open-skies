"""Ship lanes from real AIS traffic (World Bank / IMF Global Ship Density, 2015-2021, CC BY 4.0) and real ferry
routes (Overture / OSM), as a connected network the game's naval AI can path on.

Method:
  1. Read the commercial and passenger AIS density for the map (0.005 deg cells) and put it on a 500 m grid.
  2. Cost of sailing a cell: cheap where real ships go, dear where they don't, very dear in shallow water,
     impossible on land.
  3. Entry points: where heavy traffic crosses the map edge. Ports: where traffic piles up within 4 km of the coast.
  4. Least-cost paths between every pair of entry points and ports, merged and thinned into one network.
  5. Real ferry routes added and tied into the network.
Output (build dir): sealanes.bytes (NOROAD01, the format the loader reads for roads and sea lanes),
sealanes.json (lanes, ports, gateways), sealanes_preview.png.
Usage: sealanes.py <map.json> <build dir> <shipping dir> <overture dir>
"""
import collections
import json
import math
import struct
import sys
from pathlib import Path

import duckdb
import numpy as np
import rasterio
from PIL import Image, ImageDraw
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling
from scipy import ndimage
from skimage.graph import MCP_Geometric
from skimage.morphology import skeletonize

from mapgeo import MapGeo

CELL = 500.0
MIN_DEPTH = 10.0          # main lanes keep to water at least this deep
GATE_SEP_KM = 18.0
PORT_SEP_KM = 10.0


def density(geo, zip_path, tif, size):
    half = geo.ground_size_m / 2
    dst = from_origin(-half, half, CELL * geo.scale, CELL * geo.scale)
    with rasterio.open(f"/vsizip/{zip_path.as_posix()}/{tif}") as src, \
            WarpedVRT(src, crs=geo.crs(), transform=dst, width=size, height=size, resampling=Resampling.average,
                      nodata=0) as vrt:
        a = vrt.read(1).astype(np.float32)
    a[a < 0] = 0
    return a


def heights_at_cells(build, size):
    h = np.load(Path(build) / "heights.npy", mmap_mode="r")
    n = h.shape[0]
    idx = np.clip(((np.arange(size) + 0.5) * (n - 1) / size).astype(int), 0, n - 1)
    return np.asarray(h[np.ix_(idx, idx)], dtype=np.float32)


def local_peaks(score, mask, sep_px, threshold, limit):
    s = np.where(mask, score, 0)
    mx = ndimage.maximum_filter(s, size=int(sep_px) | 1)
    cand = np.argwhere((s == mx) & (s > threshold))
    cand = sorted(cand.tolist(), key=lambda rc: -s[rc[0], rc[1]])
    out = []
    for r, c in cand:
        if all(math.hypot(r - r2, c - c2) >= sep_px for r2, c2 in out):
            out.append((r, c))
        if len(out) >= limit:
            break
    return out


def trace(skel):
    """Skeleton pixels -> list of pixel chains between junctions/ends (8-connected)."""
    pts = set(map(tuple, np.argwhere(skel)))
    def nb(p):
        r, c = p
        return [(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr or dc) and (r + dr, c + dc) in pts]
    deg = {p: len(nb(p)) for p in pts}
    nodes = {p for p, d in deg.items() if d != 2}
    seen = set()
    chains = []
    for s in nodes:
        for q in nb(s):
            if (s, q) in seen:
                continue
            chain = [s, q]
            seen.add((s, q)); seen.add((q, s))
            prev, cur = s, q
            while cur not in nodes:
                nxt = [x for x in nb(cur) if x != prev and (cur, x) not in seen]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
                seen.add((prev, cur)); seen.add((cur, prev))
                chain.append(cur)
            chains.append(chain)
    return chains


def simplify(points, tol):
    from shapely.geometry import LineString
    ls = LineString(points).simplify(tol, preserve_topology=False)
    return list(ls.coords)


def chaikin(points, rounds):
    """Corner-cutting smoothing that keeps both ends where they are (so junctions stay joined)."""
    for _ in range(rounds):
        if len(points) < 3:
            return points
        out = [points[0]]
        for (x0, z0), (x1, z1) in zip(points, points[1:]):
            out += [(0.75 * x0 + 0.25 * x1, 0.75 * z0 + 0.25 * z1), (0.25 * x0 + 0.75 * x1, 0.25 * z0 + 0.75 * z1)]
        out[-1:] = [points[-1]]
        points = out
    return points


def write_noroad(path, lanes):
    buf = bytearray(b"NOROAD01") + struct.pack("<i", len(lanes))
    for cls, pts in lanes:
        buf += struct.pack("<Bfi", cls, 0.0, len(pts))
        for x, z in pts:
            buf += struct.pack("<3f", x, 0.0, z)
    Path(path).write_bytes(bytes(buf))


def main(cfg_path, build, shipping, overture):
    geo = MapGeo.load(cfg_path)
    size = int(round(geo.size_m / CELL))
    half = geo.size_m / 2
    shipping = Path(shipping)
    com = density(geo, shipping / "shipdensity_commercial_.zip", "ShipDensity_Commercial1.tif", size)
    pas = density(geo, shipping / "ShipDensity_Passenger.zip", "ShipDensity_Passenger1.tif", size)
    h = heights_at_cells(build, size)
    water = h < -2.0
    # The map's outer band slopes land down into the sea; that water is not real sea unless ships were seen there.
    fade = int(geo.cfg.get("edgeFadeMetres", 9000) / CELL) + 1
    idx = np.arange(size)
    edge = np.minimum.outer(np.minimum(idx, size - 1 - idx), np.minimum(idx, size - 1 - idx)) < fade
    water &= ~(edge & (com + pas <= 0))
    deep = water & (h < -MIN_DEPTH)

    def rank(a):
        """Percentile rank of traffic among water cells (0..1): the strait is busy everywhere, so only the
        relative order separates the real corridors from the background."""
        l = np.log1p(a)
        out = np.zeros_like(l)
        vals = l[water]
        order = np.argsort(np.argsort(vals))
        out[water] = order / max(len(vals) - 1, 1)
        return out

    dc, dp = rank(com), rank(pas)
    traffic = np.maximum(dc, 0.9 * dp)
    cost = np.where(deep, 1.0 + 150.0 * (1.0 - traffic) ** 4, np.where(water, 600.0, 1e6)).astype(np.float64)

    # Entry points on the map edge band, ports near the coast: only the strongest real traffic qualifies.
    band = np.zeros_like(water)
    band[:3, :] = band[-3:, :] = band[:, :3] = band[:, -3:] = True
    gates = local_peaks(traffic, band & deep, GATE_SEP_KM * 1000 / CELL, 0.97, 8)
    shore = ndimage.distance_transform_edt(water) * CELL
    near = water & (shore <= 4000) & (shore >= 300)
    ports = local_peaks(traffic, near, PORT_SEP_KM * 1000 / CELL, 0.97, 10)
    print(f"grid {size}x{size}; gateways {len(gates)}; ports {len(ports)}")

    # Grow one network: start at the busiest entry point, then join every other entry point and port to the
    # network by its cheapest path. Shared trunks emerge, as in real traffic, instead of a web of parallel routes.
    mask = np.zeros(cost.shape, bool)

    def connect(points):
        for p in points:
            if not mask.any():
                mask[p] = True
                continue
            m = MCP_Geometric(cost)
            costs, _ = m.find_costs([p])
            net = np.argwhere(mask)
            j = int(np.argmin(costs[net[:, 0], net[:, 1]]))
            if costs[tuple(net[j])] >= 1e5:
                continue
            for r, c in m.traceback(tuple(net[j])):
                mask[r, c] = True

    ends = sorted(gates + ports, key=lambda rc: -traffic[rc[0], rc[1]])
    connect(ends)
    # Through traffic: the busiest gateway pairs on different edges get their own direct corridor.
    def edge_of(rc):
        r, c = rc
        return min((r, "n"), (size - 1 - r, "s"), (c, "w"), (size - 1 - c, "e"))[1]
    pairs = sorted(((a, b) for i, a in enumerate(gates) for b in gates[i + 1:] if edge_of(a) != edge_of(b)),
                   key=lambda ab: -(traffic[ab[0]] + traffic[ab[1]]))[:3]
    for a, b in pairs:
        m = MCP_Geometric(cost)
        m.find_costs([a], [b])
        for r, c in m.traceback(b):
            if cost[r, c] < 1e5:
                mask[r, c] = True

    # Ferries: real routes, kept where they run over water, tied to the network by least-cost links.
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    proj = geo.crs().to_proj4()
    ferry_lines = []
    fp = Path(overture) / "ferries.parquet"
    if fp.exists():
        rows = con.execute(f"SELECT name, ST_AsText(ST_Transform(geometry, 'EPSG:4326', '{proj}', true)) "
                           f"FROM read_parquet('{fp.as_posix()}')").fetchall()
        from shapely import wkt
        for name, w in rows:
            g = wkt.loads(w)
            coords = [(x / geo.scale, z / geo.scale) for x, z in g.coords]
            px = [(int((half - z) / CELL), int((x + half) / CELL)) for x, z in coords]
            px = [(r, c) for r, c in px if 0 <= r < size and 0 <= c < size]
            wet = [p for p in px if water[p]]
            if len(wet) >= 2 and len(wet) >= 0.6 * len(px):
                ferry_lines.append((name, coords))
                # draw the ferry on the mask by connecting consecutive points with straight pixel runs
                for (r0, c0), (r1, c1) in zip(px, px[1:]):
                    steps = max(abs(r1 - r0), abs(c1 - c0), 1)
                    for t in range(steps + 1):
                        r, c = round(r0 + (r1 - r0) * t / steps), round(c0 + (c1 - c0) * t / steps)
                        if water[r, c]:
                            mask[r, c] = True
    print(f"ferry routes kept: {len(ferry_lines)}")

    # Tie each separate piece (ferry routes) into the main network by its cheapest link.
    lab, nlab = ndimage.label(mask, structure=np.ones((3, 3)))
    if nlab > 1:
        sizes = ndimage.sum(np.ones_like(lab), lab, index=range(1, nlab + 1))
        main_l = int(np.argmax(sizes)) + 1
        main = lab == main_l
        for l in range(1, nlab + 1):
            if l == main_l:
                continue
            comp = np.argwhere(lab == l)
            m = MCP_Geometric(cost)
            costs, _ = m.find_costs([tuple(p) for p in comp[:: max(1, len(comp) // 30)]])
            net = np.argwhere(main)
            j = int(np.argmin(costs[net[:, 0], net[:, 1]]))
            if costs[tuple(net[j])] < 1e5:
                for r, c in m.traceback(tuple(net[j])):
                    mask[r, c] = True

    skel = skeletonize(ndimage.binary_dilation(mask, iterations=1))
    chains = trace(skel)
    endset = set(gates + ports)
    deg = collections.Counter()
    for ch in chains:
        deg[ch[0]] += 1
        deg[ch[-1]] += 1
    lanes = []
    for ch in chains:
        if len(ch) < 2:
            continue
        dead = [p for p in (ch[0], ch[-1]) if deg[p] == 1]
        near_end = any(min(math.hypot(p[0] - e[0], p[1] - e[1]) for e in endset) <= 3 for p in dead) if endset else False
        if dead and not near_end and len(ch) * CELL < 4000:
            continue   # a short spur that leads nowhere: skeleton noise
        pts = [(-half + (c + 0.5) * CELL, half - (r + 0.5) * CELL) for r, c in ch]
        pts = chaikin(simplify(pts, 350.0), 2)
        if len(pts) >= 2:
            lanes.append((2, pts))
    km = sum(math.dist(a, b) for _, p in lanes for a, b in zip(p, p[1:])) / 1000
    shallow = sum(1 for _, p in lanes for x, z in p if h[min(size - 1, int((half - z) / CELL)), min(size - 1, int((x + half) / CELL))] > -3)
    write_noroad(Path(build) / "sealanes.bytes", lanes)

    def to_xz(rc):
        r, c = rc
        return (-half + (c + 0.5) * CELL, half - (r + 0.5) * CELL)

    Path(build, "sealanes.json").write_text(json.dumps({
        "lanes": [p for _, p in lanes], "gateways": [to_xz(g) for g in gates], "ports": [to_xz(p) for p in ports],
        "ferries": [{"name": n, "points": c} for n, c in ferry_lines]}, indent=1), encoding="utf-8")
    print(f"lanes {len(lanes)}, {km:.0f} km, points in shallow water: {shallow}")

    # Preview
    k = 4
    img = np.zeros((size, size, 3), np.uint8)
    img[~water] = (58, 64, 52)
    img[water] = (16, 28, 46)
    heat = np.clip(traffic * 255, 0, 255).astype(np.uint8)
    img[water, 0] = np.maximum(img[water, 0], heat[water] // 2)
    img[water, 1] = np.maximum(img[water, 1], heat[water] // 3)
    pic = Image.fromarray(img).resize((size * k, size * k), Image.NEAREST)
    d = ImageDraw.Draw(pic)
    for _, p in lanes:
        d.line([((x + half) / CELL * k, (half - z) / CELL * k) for x, z in p], fill=(90, 220, 255), width=3)
    for n, c in ferry_lines:
        d.line([((x + half) / CELL * k, (half - z) / CELL * k) for x, z in c], fill=(255, 210, 80), width=2)
    for r, c in gates:
        d.ellipse([c * k - 9, r * k - 9, c * k + 9, r * k + 9], outline=(255, 80, 80), width=4)
    for r, c in ports:
        d.rectangle([c * k - 7, r * k - 7, c * k + 7, r * k + 7], outline=(120, 255, 120), width=4)
    pic.save(Path(build) / "sealanes_preview.png")


if __name__ == "__main__":
    main(*sys.argv[1:])
