"""Road network for an Open Skies map, from Overture road segments (OpenStreetMap).

Writes the network the NOCustomMaps loader hands the game's ground AI (NOROAD02, the format of
NOCustomMaps Core/RoadData.cs, MIT) and a sidecar that RoadMeshes.cs reads to draw the roads.

Usage (run from tools/):
    roads.py <map.json> <build_dir> [<overture_dir>]

Inputs: <overture_dir>/roads.parquet (transportation/segment, subtype road) and infrastructure.parquet
(piers, for ports); <build_dir>/heights.npy, airbases.json and buildings.npz (towns).
Outputs in <build_dir>:
    roads.bytes        the network: game metres, x east, z north, y = height on the heights.npy datum
    roads_draw.json    per record: ribbon width, lift over the ground, kind (ground/tunnel/bridge/causeway)
    roads_stats.json   km per class, connectivity per landmass, bridges, tunnels, grades, file size
    roads_profile.npy  per record float32 [length m, worst grade, highest lift m, deepest cut m]

What goes in: motorway, trunk, primary and secondary everywhere (with their links); tertiary where it
joins parts of that network that are otherwise apart, or reaches a town or port the network misses;
roads of any class where they are the only way to join a stranded part to the rest within a few km;
one access road into every airbase pad. Nothing crosses a runway or runs through a pad. Fragments
under 1 km that connect to nothing are dropped.

Shape: records meet at junctions on identical endpoints (x, y and z), so the game builds a graph.
Ground roads are densified to 25 m, laid on the terrain mesh's own surface (the triangles MapBuilder
draws, which bilinear sampling would miss by up to metres on slopes), and their profile is smoothed
toward the grade limits within 3 m of the ground. Bridges carry their deck (clear of water by 8 m and
of land by 5 m where the ramps from their ends allow it); tunnels a straight bore between portals.
Ground roads that the DEM puts in the sea for a short run between land become low causeway decks.

Data: (c) OpenStreetMap contributors, Overture Maps Foundation (ODbL).
"""
import json
import math
import os
import struct
import sys
import time
import warnings
from pathlib import Path

# numpy and scipy each bring an OpenBLAS that commits ~250 MB of buffers for its default thread pool;
# nothing here needs BLAS, and commit is what this machine runs out of.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402
from scipy.sparse import csr_matrix  # noqa: E402
from scipy.sparse.csgraph import connected_components, dijkstra  # noqa: E402
from scipy.spatial import cKDTree  # noqa: E402

from mapgeo import MapGeo  # noqa: E402

warnings.filterwarnings("ignore", message="You will likely lose important projection information")

CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential"]
GAME_CLASS = np.array([0, 1, 2, 3, 3, 3, 3], np.uint8)
GAME_NAMES = ["motorway", "trunk", "primary", "other"]
CREDIT = "Roads (c) OpenStreetMap contributors, Overture Maps Foundation (ODbL)"

STEP = 25.0                 # densify roads to this spacing (m)
EDGE = 100.0                # keep roads this far inside the map edge
BIG = float(2 ** 20)        # per-segment offset of the global linear position (> longest segment)

GROUND, TUNNEL, BRIDGE, CAUSEWAY, SEA = 0, 1, 2, 3, 4
KIND_NAMES = ["ground", "tunnel", "bridge", "causeway"]
FLAG = {GROUND: 0, TUNNEL: 1, BRIDGE: 2, CAUSEWAY: 2}

SHORT_TUNNEL = 60.0         # underpasses shorter than this stay in their road (tunnelFraction)
SHORT_BRIDGE = 15.0         # culverts shorter than this are driven and drawn as ground
CAUSEWAY_MAX = 1500.0       # a ground road the DEM puts in the sea, between land, up to this long

GRADE = np.array([0.06, 0.08, 0.12, 0.12])          # ground profile limit by game class
BRIDGE_GRADE = np.array([0.05, 0.05, 0.08, 0.08])   # ramp from a bridge end toward its clearance
REPORT_GRADE = np.array([0.06, 0.12, 0.12, 0.12])   # what the report counts as too steep
LIFT_LIMIT = 4.0            # a ground profile rises at most this far above the terrain (fill)
CUT_LIMIT = 10.0            # and dips at most this far below it (a cut; the DEM is a surface model,
                            # so trees and buildings beside a road stand above the real road bed)
BIG_CUT = 5.0               # deeper than this is reported as a big cut
BRIDGE_END_SINK = 2.5       # a bridge end may sit this far below the terrain (never above it); the
                            # drawn deck is kept on the ground there, so vehicles meet no step
PORTAL_RANGE = (-CUT_LIMIT, 1.0)                    # a tunnel portal may be cut into the slope
SMOOTH_PASSES = np.array([30, 20, 12, 12])          # profile diffusion passes by game class
CLEAR = {BRIDGE: (8.0, 5.0), CAUSEWAY: (1.0, 0.5)}   # (over water, over land)

TERTIARY_JOIN_MAX = 30000.0
TOWN_CELL, TOWN_BUILDINGS, TOWN_REACH = 1000.0, 150, 1500.0
DEST_SNAP, DEST_PATH_MAX = 1200.0, 20000.0
STRAND_MAX, STRAND_MIN_BUDGET = 8000.0, 1500.0
FRAGMENT_MIN = 1000.0
COST = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.3, 1.6])   # path cost per metre by class, when adding roads
RUNWAY_SIDE, RUNWAY_END = 150.0, 300.0                  # no road this close to a runway (taxiway inside)

SIMPLIFY_XY, SIMPLIFY_Y, MAX_GAP = 1.0, 0.5, 200.0
MAX_RECORDS, MAX_POINTS = 100000, 1000000

T0 = time.time()


def log(msg):
    mem = memory_mb()
    print(f"[{time.time() - T0:6.1f}s{'' if mem is None else f' {mem[0]:5d}/{mem[1]:5d} MB'}] {msg}", flush=True)


def peak_memory_mb():
    m = memory_mb()
    return None if m is None else m[1]


def memory_mb():
    """(current, peak) commit (private bytes) of this process on Windows, else peak RSS twice; None if unknown."""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        f = ctypes.windll.psapi.GetProcessMemoryInfo
        f.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        f(k.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return round(c.PagefileUsage / 1e6), round(c.PeakPagefileUsage / 1e6)
    except Exception:
        try:
            import resource
            v = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3)
            return v, v
        except Exception:
            return None


# ----------------------------------------------------------------------------------------- terrain

class Terrain:
    """Heights on the terrain mesh's own surface. MapBuilder splits each grid cell along the diagonal
    from (a+1, b) to (a, b+1) (a east, b north); sampling the same triangles keeps a road on the ground
    the game draws and collides with. step = 2 or 4 gives the LOD1 / LOD2 surfaces."""

    def __init__(self, path, half, pitch):
        self.h = np.load(path)
        self.n = self.h.shape[0]
        self.half, self.pitch = half, pitch

    def __call__(self, x, z, step=1):
        x = np.asarray(x, np.float64)
        z = np.asarray(z, np.float64)
        out = np.empty(x.shape, np.float64)
        fx_, fz_ = x.ravel(), z.ravel()
        o = out.ravel()
        for a in range(0, len(fx_), 1 << 20):
            o[a:a + (1 << 20)] = self._sample(fx_[a:a + (1 << 20)], fz_[a:a + (1 << 20)], step)
        return out

    def _sample(self, x, z, step):
        p = self.pitch * step
        m = (self.n - 1) // step
        fc = (x + self.half) / p
        fb = (z + self.half) / p
        c0 = np.clip(np.floor(fc), 0, m - 1).astype(np.int64)
        b0 = np.clip(np.floor(fb), 0, m - 1).astype(np.int64)
        fx = np.clip(fc - c0, 0.0, 1.0)
        fz = np.clip(fb - b0, 0.0, 1.0)
        c0 *= step
        r0 = (self.n - 1) - b0 * step
        h = self.h
        h00, h10 = h[r0, c0], h[r0, c0 + step]
        h01, h11 = h[r0 - step, c0], h[r0 - step, c0 + step]
        low = fx + fz <= 1.0
        return np.where(low, h00 + fx * (h10 - h00) + fz * (h01 - h00),
                        h11 + (1 - fx) * (h01 - h11) + (1 - fz) * (h10 - h11))


# ----------------------------------------------------------------------------------------- input

def load_segments(con, geo, parquet):
    """Every road segment, projected to game metres, plus its connectors, flags and one-way state."""
    p = geo.crs().to_proj4()
    src = f"read_parquet('{Path(parquet).as_posix()}')"
    base = f"(SELECT (row_number() OVER (ORDER BY id) - 1)::INTEGER AS sid, * FROM {src})"
    d = con.execute(f"""SELECT sid, class, coalesce(subclass = 'link', false) AS link,
                               ST_AsWKB(ST_Transform(geometry, 'EPSG:4326', '{p}', true)) AS wkb
                        FROM {base} ORDER BY sid""").fetchnumpy()
    code = {c: i for i, c in enumerate(CLASSES)}
    cls = np.array([code[c] for c in d["class"]], np.int8)
    link = np.asarray(d["link"], bool)
    counts = np.empty(len(cls), np.int64)
    parts = []
    for i, b in enumerate(d["wkb"]):
        b = bytes(b)
        if b[0] != 1 or int.from_bytes(b[1:5], "little") != 2:
            raise ValueError(f"segment {i}: not a little-endian 2-D LineString")
        n = int.from_bytes(b[5:9], "little")
        counts[i] = n
        parts.append(np.frombuffer(b, "<f8", 2 * n, 9))
    xy = np.concatenate(parts).reshape(-1, 2) / geo.scale
    off = np.zeros(len(cls) + 1, np.int64)
    np.cumsum(counts, out=off[1:])
    del d, parts

    c = con.execute(f"""WITH u AS (SELECT sid, unnest(connectors) AS c FROM {base})
                        SELECT sid, (dense_rank() OVER (ORDER BY c.connector_id) - 1)::INTEGER AS cid,
                               c."at"::DOUBLE AS at FROM u""").fetchnumpy()
    f = con.execute(f"""WITH u AS (SELECT sid, unnest(road_flags) AS f FROM {base} WHERE road_flags IS NOT NULL),
                             v AS (SELECT sid, unnest(f."values") AS v, f."between" AS b FROM u)
                        SELECT sid, v, coalesce(b[1], 0.0)::DOUBLE AS b0, coalesce(b[2], 1.0)::DOUBLE AS b1 FROM v
                        WHERE v IN ('is_bridge', 'is_tunnel', 'is_under_construction', 'is_abandoned')""").fetchnumpy()
    o = con.execute(f"""WITH u AS (SELECT sid, unnest(access_restrictions) AS a FROM {base}
                                   WHERE access_restrictions IS NOT NULL)
                        SELECT DISTINCT sid FROM u WHERE a.access_type = 'denied'
                          AND a."when".heading IS NOT NULL AND a."when"."mode" IS NULL""").fetchnumpy()
    oneway = np.zeros(len(cls), bool)
    oneway[np.asarray(o["sid"], np.int64)] = True
    fk = {"is_tunnel": TUNNEL, "is_bridge": BRIDGE, "is_under_construction": 8, "is_abandoned": 8}
    return dict(cls=cls, link=link, oneway=oneway, xy=xy, off=off,
                c_sid=np.asarray(c["sid"], np.int64), c_cid=np.asarray(c["cid"], np.int64),
                c_at=np.asarray(c["at"], np.float64),
                f_sid=np.asarray(f["sid"], np.int64), f_kind=np.array([fk[v] for v in f["v"]], np.int8),
                f_b0=np.asarray(f["b0"], np.float64), f_b1=np.asarray(f["b1"], np.float64))


def load_pads(path):
    pads = []
    for ab in json.loads(Path(path).read_text(encoding="utf-8")):
        h = math.radians(ab["heading"])
        dx, dz = math.sin(h), math.cos(h)
        half_len = ab["runwayLength"] / 2
        runways = [(ab["x"] - dx * half_len, ab["z"] - dz * half_len, ab["x"] + dx * half_len, ab["z"] + dz * half_len,
                    ab["runwayWidth"])]
        for r in ab.get("extraRunways") or []:
            runways.append((r["startX"], r["startZ"], r["endX"], r["endZ"], r["width"]))
        pads.append(dict(name=ab["uniqueName"], x=ab["x"], z=ab["z"], dx=dx, dz=dz, along=ab["flatHalfAlong"],
                         across=ab["flatHalfAcross"], runways=runways))
    return pads


def in_pad(p, x, z):
    rx, rz = x - p["x"], z - p["z"]
    return (np.abs(rx * p["dx"] + rz * p["dz"]) <= p["along"]) & (np.abs(rx * p["dz"] - rz * p["dx"]) <= p["across"])


def on_runway(p, x, z):
    m = np.zeros(np.shape(x), bool)
    for x0, z0, x1, z1, w in p["runways"]:
        length = math.hypot(x1 - x0, z1 - z0)
        ux, uz = (x1 - x0) / length, (z1 - z0) / length
        a = (x - x0) * ux + (z - z0) * uz
        c = (x - x0) * uz - (z - z0) * ux
        m |= (a >= -RUNWAY_END) & (a <= length + RUNWAY_END) & (np.abs(c) <= w / 2 + RUNWAY_SIDE)
    return m


def town_points(build):
    """Centres of 1 km cells holding at least TOWN_BUILDINGS buildings."""
    path = build / "buildings.npz"
    if not path.exists():
        return np.zeros((0, 2))
    d = np.load(path)
    x, z = d["x"].astype(np.float64), d["z"].astype(np.float64)
    if len(x) == 0:
        return np.zeros((0, 2))
    lo = min(x.min(), z.min())
    nb = int(np.ceil((max(x.max(), z.max()) - lo) / TOWN_CELL)) + 1
    rng = [[lo, lo + nb * TOWN_CELL]] * 2
    n, _, _ = np.histogram2d(x, z, bins=nb, range=rng)
    sx, _, _ = np.histogram2d(x, z, bins=nb, range=rng, weights=x)
    sz, _, _ = np.histogram2d(x, z, bins=nb, range=rng, weights=z)
    m = n >= TOWN_BUILDINGS
    return np.column_stack([sx[m] / n[m], sz[m] / n[m]])


def port_points(con, geo, overture_dir):
    """Clusters of two or more piers in a 1 km cell: harbours."""
    path = Path(overture_dir) / "infrastructure.parquet"
    if not path.exists():
        return np.zeros((0, 2))
    p = geo.crs().to_proj4()
    d = con.execute(f"""SELECT ST_X(c) AS x, ST_Y(c) AS y FROM (
        SELECT ST_Centroid(ST_Transform(geometry, 'EPSG:4326', '{p}', true)) AS c
        FROM read_parquet('{path.as_posix()}') WHERE subtype = 'pier')""").fetchnumpy()
    x, z = np.asarray(d["x"]) / geo.scale, np.asarray(d["y"]) / geo.scale
    if not len(x):
        return np.zeros((0, 2))
    key = np.floor(x / 1000).astype(np.int64) * 100000 + np.floor(z / 1000).astype(np.int64)
    u, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    mx = np.bincount(inv, x) / cnt
    mz = np.bincount(inv, z) / cnt
    m = cnt >= 2
    return np.column_stack([mx[m], mz[m]])


# ----------------------------------------------------------------------------------------- pieces

def build_pieces(seg, pads, terr, half, fade):
    """Densify every segment, mark structure / sea / pad / dropped stretches, and cut it into pieces at
    junctions (Overture connectors) and wherever those marks change. Returns the points and the pieces."""
    xy, off = seg["xy"], seg["off"]
    S = len(off) - 1
    vs = np.repeat(np.arange(S), np.diff(off))
    d = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    keep = np.r_[True, (vs[1:] != vs[:-1]) | (d > 1e-6)]
    xy, vs = xy[keep], vs[keep]
    d = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    same = vs[1:] == vs[:-1]
    d = np.where(same, d, 0.0)
    cs = np.r_[0.0, np.cumsum(d)]
    first = np.nonzero(np.r_[True, ~same])[0]
    cum = cs - np.repeat(cs[first], np.diff(np.r_[first, len(vs)]))
    last = np.r_[first[1:] - 1, len(vs) - 1]
    L = np.zeros(S)
    L[vs[last]] = cum[last]
    if L.max() >= BIG:
        raise ValueError("a segment is longer than BIG")
    G = vs * BIG + cum

    def snap(sid, pos):
        g = sid * BIG + pos
        i = np.searchsorted(G, g)
        i0 = np.clip(i - 1, 0, len(G) - 1)
        i1 = np.clip(i, 0, len(G) - 1)
        d0 = np.where(vs[i0] == sid, np.abs(G[i0] - g), np.inf)
        d1 = np.where(vs[i1] == sid, np.abs(G[i1] - g), np.inf)
        best = np.where(d0 <= d1, i0, i1)
        return np.where(np.minimum(d0, d1) < 0.5, G[best], g), best

    fs = seg["f_sid"]
    g0, _ = snap(fs, np.clip(seg["f_b0"], 0, 1) * L[fs])
    g1, _ = snap(fs, np.clip(seg["f_b1"], 0, 1) * L[fs])
    P = np.unique(np.concatenate([G, g0, g1]))
    psid = np.floor(P / BIG).astype(np.int64)
    gap = np.diff(P)
    nsub = np.where(psid[1:] == psid[:-1], np.maximum(1, np.ceil(gap / STEP - 1e-9)), 1).astype(np.int64)
    rep = np.repeat(np.arange(len(P) - 1), nsub)
    k = np.arange(len(rep)) - np.repeat(np.cumsum(nsub) - nsub, nsub)
    Q = np.r_[P[rep] + k * (gap[rep] / nsub[rep]), P[-1]]
    del rep, k, P, psid, gap, nsub
    QX = np.interp(Q, G, xy[:, 0])
    QZ = np.interp(Q, G, xy[:, 1])
    Qsid = np.floor(Q / BIG).astype(np.int64)
    Qpos = Q - Qsid * BIG
    nq = len(Q)

    # Junctions: each connector at the segment vertex nearest its linear reference, all its
    # occurrences moved onto one coordinate (a segment end where it is one).
    cs_, cc, ca = seg["c_sid"], seg["c_cid"], seg["c_at"]
    _, vb = snap(cs_, np.clip(ca, 0, 1) * L[cs_])
    qi = np.searchsorted(Q, G[vb])
    assert np.all(Q[qi] == G[vb])
    C = int(cc.max()) + 1
    is_end = (ca <= 1e-9) | (ca >= 1 - 1e-9)
    o = np.lexsort((~is_end, cc))
    firsts = o[np.r_[True, cc[o][1:] != cc[o][:-1]]]
    cx = np.zeros(C)
    cz = np.zeros(C)
    cx[cc[firsts]] = QX[qi[firsts]]
    cz[cc[firsts]] = QZ[qi[firsts]]
    # Two connectors that land on the same point are one junction.
    parent = np.arange(C)
    o = np.argsort(qi, kind="stable")
    dup = np.nonzero(qi[o][1:] == qi[o][:-1])[0]
    merged = 0
    for j in dup:
        a, b = cc[o[j]], cc[o[j + 1]]
        while parent[a] != a:
            a = parent[a]
        while parent[b] != b:
            b = parent[b]
        if a != b:
            parent[max(a, b)] = min(a, b)
            merged += 1
    for _ in range(8):
        parent = parent[parent]
    QX[qi] = cx[parent[cc]]
    QZ[qi] = cz[parent[cc]]
    point_node = np.full(nq, -1, np.int64)
    point_node[qi] = parent[cc]

    # Marks on each interval between consecutive points.
    MG = (Q[:-1] + Q[1:]) / 2
    iv_same = Qsid[1:] == Qsid[:-1]

    def mark(rows):
        lo = np.searchsorted(MG, g0[rows])
        hi = np.searchsorted(MG, g1[rows])
        acc = np.zeros(nq, np.int32)
        np.add.at(acc, lo, 1)
        np.add.at(acc, hi, -1)
        return np.cumsum(acc)[:-1] > 0

    fk = seg["f_kind"]
    kind = np.zeros(nq - 1, np.int8)
    kind[mark(fk == TUNNEL)] = TUNNEL
    kind[mark(fk == BRIDGE)] = BRIDGE
    drop = mark(fk == 8)
    out = (np.abs(QX) > half - EDGE) | (np.abs(QZ) > half - EDGE)
    drop |= out[:-1] | out[1:]
    mx, mz = (QX[:-1] + QX[1:]) / 2, (QZ[:-1] + QZ[1:]) / 2
    pad = np.full(nq - 1, -1, np.int8)
    for kpad, p in enumerate(pads):
        pad[(pad < 0) & in_pad(p, mx, mz)] = kpad
        drop |= on_runway(p, mx, mz)
    tm = terr(mx, mz)
    # In the band where the map edge fades into the sea, nothing below water survives (not even bridges).
    drop |= (half - np.maximum(np.abs(mx), np.abs(mz)) < fade) & (tm < 0)
    kind[(kind == GROUND) & (tm < 0)] = SEA
    del mx, mz, tm, MG
    att = kind.astype(np.int32) + 8 * drop + 16 * (pad.astype(np.int32) + 1)
    att[~iv_same] = -1

    cut = point_node >= 0
    cut[0] = cut[-1] = True
    cut[1:] |= ~iv_same
    cut[:-1] |= ~iv_same
    cut[1:-1] |= (att[1:] != att[:-1]) & iv_same[1:] & iv_same[:-1]
    cuts = np.nonzero(cut)[0]
    syn = cuts[point_node[cuts] < 0]
    point_node[syn] = C + np.arange(len(syn))
    n_nodes = C + len(syn)
    # Cut points on the same spot (to 1 cm) are one junction: the game joins roads by position.
    _, first_cut, inv = np.unique(np.column_stack([np.round(QX[cuts], 2), np.round(QZ[cuts], 2)]), axis=0,
                                  return_index=True, return_inverse=True)
    canon = cuts[first_cut[inv.ravel()]]
    coincident = int((point_node[canon] != point_node[cuts]).sum())
    point_node[cuts] = point_node[canon]
    QX[cuts], QZ[cuts] = QX[canon], QZ[canon]
    pa, pb = cuts[:-1], cuts[1:]
    ok = Qsid[pa] == Qsid[pb]
    pa, pb = pa[ok], pb[ok]
    QT = terr(QX, QZ).astype(np.float32)
    pmin = np.minimum.reduceat(QT, pa)
    patt = att[pa]
    keep = (patt & 8) == 0
    pa, pb, patt, pmin = pa[keep], pb[keep], patt[keep], pmin[keep]
    psid = Qsid[pa]
    P = dict(pa=pa, pb=pb, u=point_node[pa], v=point_node[pb], kind=(patt & 7).astype(np.int8),
             pad=((patt >> 4) - 1).astype(np.int8), sid=psid, len=Qpos[pb] - Qpos[pa],
             cls=seg["cls"][psid], link=seg["link"][psid], oneway=seg["oneway"][psid],
             tun=np.zeros(len(pa), bool), pmin=pmin)

    # A piece that starts and ends on the same junction (a closed ring) is cut in two.
    loop = np.nonzero(P["u"] == P["v"])[0]
    extra = []
    for j in loop:
        a, b = P["pa"][j], P["pb"][j]
        if b - a >= 2:
            m = (a + b) // 2
            point_node[m] = n_nodes
            n_nodes += 1
            extra.append((j, m))
    if loop.size:
        dropmask = np.ones(len(pa), bool)
        dropmask[loop] = False
        add = {k_: [] for k_ in P}
        for j, m in extra:
            for a, b in ((P["pa"][j], m), (m, P["pb"][j])):
                for k_ in P:
                    add[k_].append(P[k_][j])
                add["pa"][-1], add["pb"][-1] = a, b
                add["u"][-1], add["v"][-1] = point_node[a], point_node[b]
                add["len"][-1] = Qpos[b] - Qpos[a]
        for k_ in P:
            P[k_] = np.concatenate([P[k_][dropmask], np.asarray(add[k_], P[k_].dtype)])

    # Short structures stay part of their road.
    tun = (P["kind"] == TUNNEL) & (P["len"] < SHORT_TUNNEL)
    P["tun"] = tun
    P["kind"][tun] = GROUND
    culvert = (P["kind"] == BRIDGE) & (P["len"] < SHORT_BRIDGE) & (P["pmin"] >= 0)
    P["kind"][culvert] = GROUND

    nx = np.zeros(n_nodes)
    nz = np.zeros(n_nodes)
    nx[P["u"]], nz[P["u"]] = QX[P["pa"]], QZ[P["pa"]]
    nx[P["v"]], nz[P["v"]] = QX[P["pb"]], QZ[P["pb"]]
    log(f"{S:,} segments -> {nq:,} points, {len(P['pa']):,} pieces, {n_nodes:,} nodes "
        f"({merged} connector pairs fused, {coincident} coincident cut points joined, {len(loop)} rings cut, "
        f"{tun.sum()} underpasses and "
        f"{culvert.sum()} culverts kept in their road)")
    return dict(QX=QX, QZ=QZ, QT=QT, Qpos=Qpos, n_nodes=n_nodes, nx=nx, nz=nz), P


def resolve_sea(P, n_nodes):
    """Ground pieces in the DEM's sea: a short run between land becomes a low causeway, the rest goes."""
    sea = P["kind"] == SEA
    if not sea.any():
        return np.ones(len(sea), bool), 0, 0.0
    e = np.nonzero(sea)[0]
    g = csr_matrix((np.ones(len(e)), (P["u"][e], P["v"][e])), shape=(n_nodes, n_nodes))
    _, lab = connected_components(g, directed=False)
    land_node = np.zeros(n_nodes, bool)
    land_node[P["u"][~sea]] = True
    land_node[P["v"][~sea]] = True
    clen = np.bincount(lab[P["u"][e]], P["len"][e], minlength=lab.max() + 1)
    nodes = np.unique(np.r_[P["u"][e], P["v"][e]])
    att = np.bincount(lab[nodes], land_node[nodes].astype(np.float64), minlength=lab.max() + 1)
    ok = (clen <= CAUSEWAY_MAX) & (att >= 2)
    become = ok[lab[P["u"][e]]]
    P["kind"][e[become]] = CAUSEWAY
    keep = np.ones(len(sea), bool)
    keep[e[~become]] = False
    return keep, int(become.sum()), float(P["len"][e[~become]].sum())


# ----------------------------------------------------------------------------------------- graphs

class EdgeGraph:
    """Undirected graph over a subset of pieces, for shortest paths back to a set of sources."""

    def __init__(self, n, edges, P, weight):
        a = np.minimum(P["u"][edges], P["v"][edges])
        b = np.maximum(P["u"][edges], P["v"][edges])
        key = a.astype(np.int64) * n + b
        o = np.lexsort((weight, key))
        key, a, b, w, eid = key[o], a[o], b[o], weight[o], edges[o]
        f = np.r_[True, key[1:] != key[:-1]]
        self.key, self.eid, self.n = key[f], eid[f], n
        self.g = csr_matrix((np.maximum(w[f], 1e-3), (a[f], b[f])), shape=(n, n))

    def run(self, sources, limit=np.inf):
        return dijkstra(self.g, directed=False, indices=np.asarray(sources, np.int64), return_predecessors=True,
                        min_only=True, limit=limit)

    def path(self, pred, node):
        out = []
        node = int(node)
        while pred[node] >= 0:
            p = int(pred[node])
            k = min(p, node) * self.n + max(p, node)
            i = int(np.searchsorted(self.key, k))
            assert self.key[i] == k
            out.append(int(self.eid[i]))
            node = p
        return out


def components(n, P, edges):
    g = csr_matrix((np.ones(len(edges)), (P["u"][edges], P["v"][edges])), shape=(n, n))
    return connected_components(g, directed=False)[1]


def land_groups(terr, P, keep, nx, nz):
    """Land cells labelled into islands; islands that any road crosses between are one group."""
    lab, _ = ndimage.label(terr.h >= 0)
    n = terr.n
    col = np.clip(np.rint((nx + terr.half) / terr.pitch), 0, n - 1).astype(np.int64)
    row = np.clip((n - 1) - np.rint((nz + terr.half) / terr.pitch), 0, n - 1).astype(np.int64)
    nl = lab[row, col]
    parent = {}

    def find(a):
        while parent.get(a, a) != a:
            a = parent[a]
        return a

    la, lb = nl[P["u"][keep]], nl[P["v"][keep]]
    m = (la > 0) & (lb > 0) & (la != lb)
    for a, b in set(zip(la[m].tolist(), lb[m].tolist())):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    labels = np.unique(nl)
    root = np.array([find(int(v)) if v > 0 else -1 for v in labels])
    node_group = root[np.searchsorted(labels, nl)]
    del lab
    return node_group


def select(P, A, pads, towns, ports, groups):
    """Choose the pieces that make the network (see the module docstring)."""
    n, nx, nz = A["n_nodes"], A["nx"], A["nz"]
    m = len(P["u"])
    idx = np.arange(m)
    usable = P["alive"] & (P["pad"] < 0)
    sel = usable & (P["cls"] <= 3)
    km0 = P["len"][sel].sum() / 1000
    report = {"base_km": round(km0, 1)}

    # Airbase access: the best way in from the pad edge to the road point nearest the runway centre.
    access = []
    outside = np.zeros(n, bool)
    outside[P["u"][usable]] = True
    outside[P["v"][usable]] = True
    for k, p in enumerate(pads):
        e = idx[P["alive"] & (P["pad"] == k)]
        if not len(e):
            continue
        nodes = np.unique(np.r_[P["u"][e], P["v"][e]])
        edge_nodes = nodes[outside[nodes]]
        if not len(edge_nodes):
            continue
        g = EdgeGraph(n, e, P, P["len"][e] * COST[P["cls"][e]])
        dist, pred, _ = g.run(edge_nodes)
        reached = nodes[np.isfinite(dist[nodes])]
        t = reached[np.argmin(np.hypot(nx[reached] - p["x"], nz[reached] - p["z"]))]
        path = g.path(pred, t)
        access += path
        # Where the way in ends (the pad edge itself when no road inside the pad leads anywhere).
        report.setdefault("access", {})[p["name"]] = dict(
            inside_m=round(float(P["len"][path].sum())), end_x=round(float(nx[t])), end_z=round(float(nz[t])),
            end_from_runway_centre_m=round(float(np.hypot(nx[t] - p["x"], nz[t] - p["z"]))))
    access = np.array(sorted(set(access)), np.int64)
    sel[access] = True
    usable[access] = True

    # Tertiary roads that join separate parts of the main network (cheapest joins first).
    tert = idx[usable & (P["cls"] == 4)]
    base = idx[sel]
    lab = components(n, P, base)
    clen = np.bincount(lab[P["u"][base]], P["len"][base], minlength=lab.max() + 1)
    gt = EdgeGraph(n, tert, P, P["len"][tert])
    base_nodes = np.unique(np.r_[P["u"][base], P["v"][base]])
    dist, pred, src = gt.run(base_nodes, TERTIARY_JOIN_MAX)
    a, b = P["u"][tert], P["v"][tert]
    ok = np.isfinite(dist[a]) & np.isfinite(dist[b])
    ca = np.where(ok, lab[np.where(ok, src[a], 0)], -1)
    cb = np.where(ok, lab[np.where(ok, src[b], 0)], -1)
    total = np.where(ok, dist[a] + dist[b], np.inf) + P["len"][tert]
    ok &= (ca != cb) & (total <= TERTIARY_JOIN_MAX)
    parent = {}
    size = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    added = []
    joins = 0
    for i in np.nonzero(ok)[0][np.argsort(total[ok], kind="stable")]:
        ra, rb = find(int(ca[i])), find(int(cb[i]))
        if ra == rb:
            continue
        sa, sb = size.get(ra, clen[ra]), size.get(rb, clen[rb])
        if total[i] > 2000 + 2 * min(sa, sb):
            continue
        parent[max(ra, rb)] = min(ra, rb)
        size[min(ra, rb)] = sa + sb + total[i]
        added += gt.path(pred, a[i]) + [int(tert[i])] + gt.path(pred, b[i])
        joins += 1
    added = np.array(sorted(set(added)), np.int64)
    sel[added] = True
    report["tertiary_joins"] = joins
    report["tertiary_join_km"] = round(P["len"][added].sum() / 1000, 1)

    # Tertiary roads to towns and ports the network does not reach.
    dests = np.vstack([towns.reshape(-1, 2), ports.reshape(-1, 2)])
    cur = idx[sel]
    pts = piece_points(P, cur, 4)
    tree = cKDTree(np.column_stack([A["QX"][pts], A["QZ"][pts]]))
    far = dests[tree.query(dests)[0] > TOWN_REACH] if len(dests) else dests
    cur_nodes = np.unique(np.r_[P["u"][cur], P["v"][cur]])
    dist, pred, _ = gt.run(cur_nodes, DEST_PATH_MAX)
    tnodes = np.unique(np.r_[P["u"][tert], P["v"][tert]])
    tnodes = tnodes[np.isfinite(dist[tnodes])]
    reached_dest = 0
    added = []
    if len(tnodes) and len(far):
        tt = cKDTree(np.column_stack([nx[tnodes], nz[tnodes]]))
        dd, ii = tt.query(far, distance_upper_bound=DEST_SNAP)
        for d_, i_ in zip(dd, ii):
            if np.isfinite(d_):
                added += gt.path(pred, tnodes[i_])
                reached_dest += 1
    added = np.array(sorted(set(added)), np.int64)
    new = added[~sel[added]] if len(added) else added
    sel[added] = True
    report["destinations"] = int(len(dests))
    report["destinations_far"] = int(len(far))
    report["destinations_linked"] = reached_dest
    report["tertiary_dest_km"] = round(P["len"][new].sum() / 1000, 1)

    # Stranded parts: join each to the largest part of its landmass by the cheapest roads of any class.
    cur = idx[sel]
    lab = components(n, P, cur)
    grp = groups[P["u"][cur]]
    grp = np.where(grp >= 0, grp, groups[P["v"][cur]])
    cl = lab[P["u"][cur]]
    comp_len = np.bincount(cl, P["len"][cur], minlength=lab.max() + 1)
    main = set()
    for gval in np.unique(grp[grp >= 0]):
        mm = grp == gval
        per = np.bincount(cl[mm], P["len"][cur][mm], minlength=lab.max() + 1)
        main.add(int(per.argmax()))
    main_arr = np.array(sorted(main))
    cand = idx[usable]
    w = P["len"][cand] * np.where(sel[cand], 0.02, COST[P["cls"][cand]])
    ga = EdgeGraph(n, cand, P, w)
    cur_nodes = np.unique(np.r_[P["u"][cur], P["v"][cur]])
    src_nodes = cur_nodes[np.isin(lab[cur_nodes], main_arr)]
    dist, pred, _ = ga.run(src_nodes, STRAND_MAX)
    others = cur_nodes[~np.isin(lab[cur_nodes], main_arr)]
    o = np.lexsort((dist[others], lab[others]))
    others = others[o]
    firsts = others[np.r_[True, lab[others][1:] != lab[others][:-1]]] if len(others) else others
    added = []
    joined = 0
    for node in firsts:
        budget = min(STRAND_MAX, max(STRAND_MIN_BUDGET, comp_len[lab[node]]))
        if dist[node] <= budget:
            added += ga.path(pred, node)
            joined += 1
    added = np.array(sorted(set(added)), np.int64)
    new = added[~sel[added]] if len(added) else added
    sel[added] = True
    report["stranded_parts"] = int(len(firsts))
    report["stranded_joined"] = joined
    report["stranded_join_km"] = round(P["len"][new].sum() / 1000, 1)
    report["stranded_join_km_by_class"] = {CLASSES[c]: round(P["len"][new][P["cls"][new] == c].sum() / 1000, 1)
                                           for c in range(7) if (P["cls"][new] == c).any()}

    # Fragments under 1 km that still connect to nothing.
    cur = idx[sel]
    lab = components(n, P, cur)
    cl = lab[P["u"][cur]]
    comp_len = np.bincount(cl, P["len"][cur], minlength=lab.max() + 1)
    small = comp_len[cl] < FRAGMENT_MIN
    sel[cur[small]] = False
    report["fragments_dropped"] = int(len(np.unique(cl[small])))
    report["fragments_dropped_km"] = round(P["len"][cur[small]].sum() / 1000, 1)
    log("selection: " + json.dumps(report))
    return idx[sel], report


def piece_points(P, edges, stride=1):
    """Indices of the points of the given pieces (every stride-th, plus both ends)."""
    a, b = P["pa"][edges], P["pb"][edges]
    cnt = b - a + 1
    rep = np.repeat(a, cnt)
    k = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    pts = rep + k
    end = np.repeat(b, cnt)
    return pts[(k % stride == 0) | (pts == end)]


# ----------------------------------------------------------------------------------------- records

def width_of(cls, link, oneway):
    g = GAME_CLASS[cls]
    w = np.select([g == 0, g == 1, g == 2], [24.0, 16.0, 12.0], 8.0)
    w = np.where(cls >= 5, 7.0, w)
    w = np.where(oneway, np.select([g == 0, g == 1, g == 2], [13.0, 9.0, 7.0], 6.0), w)
    return np.where(link, np.where(g <= 1, 8.0, 7.0), w)


def lift_of(cls, link):
    g = GAME_CLASS[cls].astype(np.float64)
    # Higher classes sit a little higher, so overlapping ribbons at junctions do not fight for depth.
    return np.where(link, 0.33, 0.30 + 0.06 * (3 - g))


def walk(n_edges, u, v, key):
    """Maximal chains through nodes of degree 2 whose two edges share a key. Returns lists of
    (edge, forward)."""
    inc = {}
    for j in range(n_edges):
        inc.setdefault(int(u[j]), []).append(j)
        inc.setdefault(int(v[j]), []).append(j)

    def through(node):
        e = inc[node]
        return len(e) == 2 and e[0] != e[1] and key[e[0]] == key[e[1]]

    used = np.zeros(n_edges, bool)
    chains = []

    def follow(start, j):
        chain = []
        node = start
        while True:
            used[j] = True
            fwd = u[j] == node
            chain.append((j, bool(fwd)))
            node = int(v[j] if fwd else u[j])
            if node == start or not through(node):
                return chain
            e = inc[node]
            j = e[1] if e[0] == j else e[0]
            if used[j]:
                return chain

    for node, e in inc.items():
        if through(node):
            continue
        for j in e:
            if not used[j]:
                chains.append(follow(node, j))
    for j in range(n_edges):
        if not used[j]:
            chains.append(follow(int(u[j]), j))
    return chains


def make_records(P, A, sel):
    """Merge the chosen pieces into records between junctions, one class / kind / width each."""
    u, v = P["u"][sel], P["v"][sel]
    width = width_of(P["cls"][sel], P["link"][sel], P["oneway"][sel])
    gcl = GAME_CLASS[P["cls"][sel]]
    wcode = np.searchsorted(np.unique(width), width)
    key = (gcl.astype(np.int64) * 1000 + P["kind"][sel] * 100 + wcode).tolist()
    chains = walk(len(sel), u, v, key)
    recs = []
    for ch in chains:
        parts, tun = [], []
        for k, (j, fwd) in enumerate(ch):
            pj = sel[j]
            a, b = P["pa"][pj], P["pb"][pj]
            r = np.arange(a, b + 1) if fwd else np.arange(b, a - 1, -1)
            if k:
                r = r[1:]
            parts.append(r)
            tun.append(np.full(len(r) - (0 if k else 1), P["tun"][pj]))
        j0, f0 = ch[0]
        j1, f1 = ch[-1]
        n0 = int(u[j0] if f0 else v[j0])
        n1 = int(v[j1] if f1 else u[j1])
        pj = sel[j0]
        recs.append(dict(idx=np.concatenate(parts), tun=np.concatenate(tun), u=n0, v=n1,
                         kind=int(P["kind"][pj]), cls=int(P["cls"][pj]), gcl=int(gcl[j0]),
                         width=float(width[j0]), lift=float(lift_of(P["cls"][pj], P["link"][pj])),
                         ccls=np.concatenate([np.full(len(p_), P["cls"][sel[j]]) for p_, (j, _) in zip(parts, ch)])))
    # A record that closes on itself is cut in two at its middle point.
    n_nodes = A["n_nodes"]
    out = []
    for r in recs:
        if r["u"] == r["v"] and len(r["idx"]) >= 3:
            m = len(r["idx"]) // 2
            mid = n_nodes
            n_nodes += 1
            for sl, tl, a, b in ((slice(0, m + 1), slice(0, m), r["u"], mid), (slice(m, None), slice(m, None), mid, r["v"])):
                q = dict(r)
                q.update(idx=r["idx"][sl], tun=r["tun"][tl], u=a, v=b, ccls=r["ccls"][sl])
                out.append(q)
        elif r["u"] != r["v"]:
            out.append(r)
    for r in out:
        r["x"] = A["QX"][r["idx"]]
        r["z"] = A["QZ"][r["idx"]]
        r["t"] = A["QT"][r["idx"]].astype(np.float64)
        r["s"] = np.r_[0.0, np.cumsum(np.hypot(np.diff(r["x"]), np.diff(r["z"])))]
    return out


def orient_runs(recs):
    """startAlong: metres of road before each record along the run of records it continues
    (through nodes where exactly two records meet); records are flipped to run one way."""
    u = np.array([r["u"] for r in recs])
    v = np.array([r["v"] for r in recs])
    chains = walk(len(recs), u, v, [0] * len(recs))
    for ch in chains:
        along = 0.0
        for j, fwd in ch:
            r = recs[j]
            if not fwd:
                for k in ("idx", "x", "z", "t", "ccls"):
                    r[k] = r[k][::-1].copy()
                r["tun"] = r["tun"][::-1].copy()
                r["u"], r["v"] = r["v"], r["u"]
                r["s"] = r["s"][-1] - r["s"][::-1]
            r["start_along"] = along
            along += r["s"][-1]
    return len(chains)


# ----------------------------------------------------------------------------------------- heights

def node_heights(recs, terr, prior=None):
    """Heights of the junctions. Where a ground road meets, the terrain (or prior, the smoothed ground
    profile); junctions where only bridges meet float at their clearance, interpolated along the decks
    from the ends; junctions inside tunnels are interpolated along the bores."""
    nodes = np.unique([r["u"] for r in recs] + [r["v"] for r in recs])
    nid = {int(n): i for i, n in enumerate(nodes)}
    N = len(nodes)
    ru = np.array([nid[r["u"]] for r in recs])
    rv = np.array([nid[r["v"]] for r in recs])
    kind = np.array([r["kind"] for r in recs])
    rlen = np.array([max(r["s"][-1], 1.0) for r in recs])
    deg = np.bincount(ru, minlength=N) + np.bincount(rv, minlength=N)
    has = {k: (np.bincount(ru, kind == k, minlength=N) + np.bincount(rv, kind == k, minlength=N)) > 0
           for k in (GROUND, TUNNEL, BRIDGE, CAUSEWAY)}
    x = np.zeros(N)
    z = np.zeros(N)
    for r, a, b in zip(recs, ru, rv):
        x[a], z[a] = r["x"][0], r["z"][0]
        x[b], z[b] = r["x"][-1], r["z"][-1]
    g = terr(x, z)
    deck = has[BRIDGE] | has[CAUSEWAY]
    grounded = has[GROUND] | (has[TUNNEL] & deck) | (deg <= 1)
    surf = np.maximum(g, 0.0)
    req = np.full(N, -np.inf)
    for k in (BRIDGE, CAUSEWAY):
        wv, lv = CLEAR[k]
        req = np.where(has[k], np.maximum(req, surf + np.where(g < 0, wv, lv)), req)
    gy = np.where(g < 0, 1.0, g)
    if prior is not None:
        gy = np.array([prior.get(int(nd), gy[i]) for i, nd in enumerate(nodes)])
    y = np.where(grounded, gy, np.where(deck, req, g))
    free = ~grounded
    w = 1.0 / rlen
    for _ in range(600):
        num = np.bincount(ru, w * y[rv], minlength=N) + np.bincount(rv, w * y[ru], minlength=N)
        den = np.bincount(ru, w, minlength=N) + np.bincount(rv, w, minlength=N)
        yn = np.where(free, num / np.maximum(den, 1e-12), y)
        yn = np.where(free & deck, np.maximum(yn, req), yn)
        if np.abs(yn - y).max() < 1e-3:
            y = yn
            break
        y = yn
    return {int(n): float(y[i]) for i, n in enumerate(nodes)}, int(free.sum())


def ground_profiles(recs, nodey):
    """Smooth the ground roads' profiles toward their grade limits, within LIFT_LIMIT above and CUT_LIMIT
    below the terrain. Junctions where only ground roads meet move with the roads (one height shared by
    all of them); junctions at a bridge, causeway or tunnel end stay on the terrain."""
    G = [r for r in recs if r["kind"] == GROUND]
    if not G:
        return
    kinds = {}
    for r in recs:
        for nd in (r["u"], r["v"]):
            kinds.setdefault(nd, set()).add(r["kind"])
    t = np.concatenate([r["t"] for r in G])
    s = np.concatenate([r["s"] for r in G])
    n = np.array([len(r["t"]) for r in G])
    start = np.r_[0, np.cumsum(n)[:-1]]
    stop = start + n - 1
    nodes = sorted({r["u"] for r in G} | {r["v"] for r in G})
    nid = {v: i for i, v in enumerate(nodes)}
    end_pos = np.r_[start, stop]
    end_node = np.array([nid[r["u"]] for r in G] + [nid[r["v"]] for r in G])
    nbr_pos = np.r_[start + 1, stop - 1]
    cnt = np.bincount(end_node, minlength=len(nodes)).astype(np.float64)
    # How far each junction may leave the terrain: ground-only ones like the road, tunnel portals into
    # the slope, bridge ends only down (buried), causeway ends not at all.
    rlo = np.full(len(nodes), -CUT_LIMIT)
    rhi = np.full(len(nodes), LIFT_LIMIT)
    for i, v in enumerate(nodes):
        k = kinds[v]
        if TUNNEL in k:
            rlo[i], rhi[i] = max(rlo[i], PORTAL_RANGE[0]), min(rhi[i], PORTAL_RANGE[1])
        if BRIDGE in k:
            rlo[i], rhi[i] = max(rlo[i], -BRIDGE_END_SINK), min(rhi[i], 0.0)
        if CAUSEWAY in k:
            rlo[i], rhi[i] = 0.0, 0.0
    free_node = rhi > rlo
    gcl = np.array([r["gcl"] for r in G])
    node_passes = np.zeros(len(nodes))
    np.maximum.at(node_passes, end_node, np.r_[SMOOTH_PASSES[gcl], SMOOTH_PASSES[gcl]])
    ynode = np.array([nodey[v] for v in nodes])
    tnode = ynode.copy()
    nlo, nhi = tnode + rlo, tnode + rhi
    within = np.ones(len(t) - 1, bool)
    within[stop[:-1]] = False
    ds = np.where(within, np.diff(s), 0.0)
    lim = np.repeat(GRADE[gcl], n)[:-1] * ds
    passes = np.repeat(SMOOTH_PASSES[gcl], n)
    lo, hi = t - CUT_LIMIT, t + LIFT_LIMIT
    inner = np.ones(len(t), bool)
    inner[end_pos] = False
    y = t.copy()
    y[end_pos] = ynode[end_node]

    # Diffusion first (about 60 m wide, 100 m on motorways): the DEM's 30 m noise is not the road.
    for it in range(int(SMOOTH_PASSES.max())):
        avg = y.copy()
        avg[1:-1] = 0.5 * (y[:-2] + y[2:])
        y = np.clip(np.where(inner & (passes > it), 0.5 * (y + avg), y), lo, hi)
        nb = np.bincount(end_node, y[nbr_pos], minlength=len(nodes)) / cnt
        ynode = np.clip(np.where(free_node & (node_passes > it), 0.5 * (ynode + nb), ynode), nlo, nhi)
        y[end_pos] = ynode[end_node]

    free = inner.copy()
    free[end_pos] = free_node[end_node]
    fl, fr = free[:-1], free[1:]
    sl = np.where(fl & fr, 0.5, np.where(fl, 1.0, 0.0))
    sr = np.where(fl & fr, 0.5, np.where(fr, 1.0, 0.0))
    best = np.inf
    stall = 0
    for it in range(3000):
        dy = np.diff(y)
        ex = np.abs(dy) - lim
        viol = within & (ex > 1e-3)
        total = ex[viol].sum()
        if total < 1e-3:
            break
        stall = stall + 1 if total > best * 0.999 else 0
        best = min(best, total)
        if stall > 50:
            break
        c = np.where(viol, np.sign(dy) * ex * 0.5, 0.0)
        corr = np.zeros_like(y)
        corr[:-1] += c * sl
        corr[1:] -= c * sr
        ncorr = np.bincount(end_node, corr[end_pos], minlength=len(nodes)) / cnt
        y = np.clip(y + corr, lo, hi)
        ynode = np.clip(ynode + np.where(free_node, ncorr, 0.0), nlo, nhi)
        y[end_pos] = ynode[end_node]
    for v, i in nid.items():
        nodey[v] = float(ynode[i])
    for r, a, k in zip(G, start, n):
        r["y"] = y[a:a + k].copy()
        # Underpasses kept in the road run straight between their ends.
        if r["tun"].any():
            m = r["tun"]
            edges = np.diff(np.r_[0, m.astype(np.int8), 0])
            for i0, i1 in zip(np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]):
                ss = r["s"][i0:i1 + 1]
                r["y"][i0:i1 + 1] = np.interp(ss, [ss[0], ss[-1]], [r["y"][i0], r["y"][i1]])
    return it


def structure_profiles(recs, nodey):
    short = []
    for r in recs:
        if r["kind"] == GROUND:
            continue
        ya, yb = nodey[r["u"]], nodey[r["v"]]
        s, g = r["s"], r["t"]
        length = max(s[-1], 1e-6)
        lin = ya + (yb - ya) * s / length
        if r["kind"] == TUNNEL:
            r["y"] = lin
            continue
        wv, lv = CLEAR[r["kind"]]
        surf = np.maximum(g, 0.0)
        req = surf + np.where(g < 0, wv, lv)
        gb = BRIDGE_GRADE[r["gcl"]]
        up = np.minimum(ya + gb * s, yb + gb * (length - s))
        target = np.minimum(req, up)
        y = np.maximum(lin, target)
        for _ in range(30):
            if len(y) > 2:
                y[1:-1] = 0.25 * y[:-2] + 0.5 * y[1:-1] + 0.25 * y[2:]
            y = np.maximum(y, target)
            y[0], y[-1] = ya, yb
        if len(y) > 2:
            y[1:-1] = np.maximum(y[1:-1], np.minimum(surf + 0.3, up)[1:-1])
        r["y"] = y
        if r["kind"] == BRIDGE and len(y) > 2:
            gap = (req - y)[1:-1]
            wet = (g < 0)[1:-1]
            i = len(y) // 2
            for m, where in ((wet, "water"), (~wet, "land")):
                if m.any() and gap[m].max() > 1.0:
                    short.append((where, float(gap[m].max()), float(s[-1]), float(r["x"][i]), float(r["z"][i])))
    return short


# ----------------------------------------------------------------------------------------- simplify

def simplify(x, z, y, s):
    n = len(x)
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        dx, dz, dy = x[b] - x[a], z[b] - z[a], y[b] - y[a]
        L2 = dx * dx + dz * dz
        px, pz = x[a + 1:b] - x[a], z[a + 1:b] - z[a]
        t = np.clip((px * dx + pz * dz) / L2, 0.0, 1.0) if L2 > 1e-9 else np.zeros(b - a - 1)
        err = np.maximum(np.hypot(px - t * dx, pz - t * dz) / SIMPLIFY_XY,
                         np.abs(y[a + 1:b] - (y[a] + t * dy)) / SIMPLIFY_Y)
        i = int(np.argmax(err))
        if err[i] > 1.0:
            m = a + 1 + i
        elif s[b] - s[a] > MAX_GAP:
            m = int(np.searchsorted(s, 0.5 * (s[a] + s[b])))
            m = min(max(m, a + 1), b - 1)
        else:
            continue
        keep[m] = True
        stack.append((a, m))
        stack.append((m, b))
    return np.nonzero(keep)[0]


# ----------------------------------------------------------------------------------------- file

def write_roads(path, recs):
    """recs: dicts with cls (0-3), flags, tunnel_fraction, start_along, pts (n x 3 float32: x, y, z)."""
    v2 = any(r["flags"] or r["start_along"] for r in recs)
    with open(path, "wb") as f:
        f.write(b"NOROAD02" if v2 else b"NOROAD01")
        f.write(struct.pack("<i", len(recs)))
        for r in recs:
            if v2:
                f.write(struct.pack("<BBffi", r["cls"], r["flags"], r["tunnel_fraction"], r["start_along"], len(r["pts"])))
            else:
                f.write(struct.pack("<Bfi", r["cls"], r["tunnel_fraction"], len(r["pts"])))
            f.write(np.ascontiguousarray(r["pts"], "<f4").tobytes())


def read_roads(path):
    b = Path(path).read_bytes()
    magic = b[:8]
    if magic not in (b"NOROAD01", b"NOROAD02"):
        raise ValueError("not road data")
    v2 = magic == b"NOROAD02"
    n = struct.unpack_from("<i", b, 8)[0]
    o = 12
    out = []
    for _ in range(n):
        if v2:
            c, fl, tf, sa, pc = struct.unpack_from("<BBffi", b, o)
            o += 14
        else:
            c, tf, pc = struct.unpack_from("<Bfi", b, o)
            fl, sa = 0, 0.0
            o += 9
        pts = np.frombuffer(b, "<f4", pc * 3, o).reshape(-1, 3)
        o += pc * 12
        out.append(dict(cls=c, flags=fl, tunnel_fraction=tf, start_along=sa, pts=pts))
    if o != len(b):
        raise ValueError(f"{len(b) - o} trailing bytes")
    return out


# ----------------------------------------------------------------------------------------- main

def main(cfg_path, build_dir, overture_dir=None):
    from overture import connect
    geo = MapGeo.load(cfg_path)
    build = Path(build_dir)
    ov = Path(overture_dir) if overture_dir else Path(__file__).resolve().parents[1] / "data" / "overture" / Path(build).name
    half = geo.size_m / 2
    terr = Terrain(build / "heights.npy", half, geo.cfg["vertexPitchMetres"])
    pads = load_pads(build / "airbases.json")
    con = connect()
    seg = load_segments(con, geo, ov / "roads.parquet")
    ports = port_points(con, geo, ov)
    con.close()
    if len(seg["cls"]) == 0:
        # No mapped roads in the area (open sea, a remote island): no road network, and no stale one either.
        for f in ("roads.bytes", "roads_draw.json", "roads_profile.npy", "roads_profile_ground.npy"):
            (build / f).unlink(missing_ok=True)
        log("no roads in this area: the map ships without a road network")
        return
    log(f"loaded {len(seg['cls']):,} segments, {len(seg['xy']):,} vertices, {len(seg['c_cid']):,} connector uses, "
        f"{len(seg['f_kind']):,} flags; {len(ports)} harbours")
    towns = town_points(build)
    log(f"{len(towns)} town cells (>= {TOWN_BUILDINGS} buildings per km2)")

    A, P = build_pieces(seg, pads, terr, half, geo.cfg.get("edgeFadeMetres", 9000))
    del seg
    alive, n_cause, sea_dropped = resolve_sea(P, A["n_nodes"])
    P["alive"] = alive
    log(f"{n_cause} pieces in the DEM's sea become causeways; {sea_dropped / 1000:.1f} km over open sea dropped")
    groups = land_groups(terr, P, alive, A["nx"], A["nz"])
    sel, report = select(P, A, pads, towns, ports, groups)

    recs = make_records(P, A, sel)
    runs = orient_runs(recs)
    log(f"{len(recs):,} records in {runs:,} runs")
    nodey, n_free = node_heights(recs, terr)
    iters = ground_profiles(recs, nodey)
    nodey, n_free = node_heights(recs, terr, prior=nodey)
    short = structure_profiles(recs, nodey)
    log(f"heights: profile settled in {iters} passes; {n_free} nodes on structures solved; bridges short of clearance by > 1 m: "
        f"{sum(1 for s_ in short if s_[0] == 'water')} over water, {sum(1 for s_ in short if s_[0] == 'land')} over land")

    out = []
    prof = np.zeros((len(recs), 4), np.float32)
    for i, r in enumerate(recs):
        y = r["y"]
        y[0], y[-1] = nodey[r["u"]], nodey[r["v"]]
        keep = simplify(r["x"], r["z"], y, r["s"])
        pts = np.column_stack([r["x"][keep], y[keep], r["z"][keep]]).astype(np.float32)
        length = float(r["s"][-1])
        tf = 1.0 if r["kind"] == TUNNEL else (float((np.diff(r["s"]) * r["tun"]).sum() / length) if length else 0.0)
        out.append(dict(cls=r["gcl"], flags=FLAG[r["kind"]], tunnel_fraction=np.float32(tf),
                        start_along=np.float32(r["start_along"]), pts=pts))
        ds = np.diff(r["s"][keep])
        gr = np.abs(np.diff(y[keep])) / np.maximum(ds, 1e-6)
        dev = y - r["t"]
        prof[i] = (length, gr[ds >= 10].max() if (ds >= 10).any() else 0.0, max(dev.max(), 0), max(-dev.min(), 0))
    if len(out) > MAX_RECORDS or max(len(o["pts"]) for o in out) > MAX_POINTS:
        raise ValueError("over the loader's limits")
    path = build / "roads.bytes"
    write_roads(path, out)
    back = read_roads(path)
    assert len(back) == len(out)
    for a, b in zip(out, back):
        assert a["cls"] == b["cls"] and a["flags"] == b["flags"] and np.array_equal(a["pts"], b["pts"])
        assert np.float32(a["tunnel_fraction"]) == np.float32(b["tunnel_fraction"])
        assert np.float32(a["start_along"]) == np.float32(b["start_along"])
    log(f"wrote {path} ({path.stat().st_size / 1e6:.2f} MB), read back identical")

    draw = dict(version=1, credit=CREDIT, count=len(recs),
                width=[round(r["width"], 1) for r in recs], lift=[round(r["lift"], 2) for r in recs],
                kind=[r["kind"] for r in recs])
    (build / "roads_draw.json").write_text(json.dumps(draw, separators=(",", ":")), encoding="utf-8")
    np.save(build / "roads_profile.npy", prof)
    # The smoothed ground profiles at full density, for a terrain pass that wants to bed the roads in:
    # float32 [x, z, profile y, half width] per point.
    gp = [np.column_stack([r["x"], r["z"], r["y"], np.full(len(r["x"]), r["width"] / 2)]) for r in recs if r["kind"] == GROUND]
    np.save(build / "roads_profile_ground.npy", np.concatenate(gp).astype(np.float32))
    stats = statistics(out, recs, prof, nodey, groups, A, terr, geo, report, short, path)
    (build / "roads_stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    log("stats: " + json.dumps({k: stats[k] for k in ("records", "points", "km_total", "km_by_class",
                                                      "connectivity", "file_mb")}))
    log(f"peak commit {peak_memory_mb()} MB")


def statistics(out, recs, prof, nodey, node_group, A, terr, geo, report, short, path):
    def km(sel):
        return round(sum(r["s"][-1] for r, m in zip(recs, sel) if m) / 1000, 1)

    kinds = np.array([r["kind"] for r in recs])
    gcl = np.array([r["gcl"] for r in recs])
    length = np.array([r["s"][-1] for r in recs])
    st = dict(records=len(out), points=int(sum(len(o["pts"]) for o in out)),
              file_mb=round(path.stat().st_size / 1e6, 2), km_total=round(length.sum() / 1000, 1))
    st["km_by_class"] = {GAME_NAMES[c]: round(length[gcl == c].sum() / 1000, 1) for c in range(4)}
    by_src = np.zeros(7)
    for r in recs:
        seg_len = np.r_[np.diff(r["s"]), 0.0]
        by_src += np.bincount(r["ccls"], seg_len, minlength=7)
    st["km_by_overture_class"] = {CLASSES[c]: round(by_src[c] / 1000, 1) for c in range(7)}
    st["structures"] = {KIND_NAMES[k]: {"records": int((kinds == k).sum()), "km": round(length[kinds == k].sum() / 1000, 1),
                                        "longest_m": round(float(length[kinds == k].max()), 0) if (kinds == k).any() else 0}
                        for k in (TUNNEL, BRIDGE, CAUSEWAY)}
    st["partly_tunnel_records"] = int(sum(1 for o in out if 0 < o["tunnel_fraction"] < 1))
    seg_pts = np.concatenate([np.diff(o["pts"][:, [0, 2]], axis=0) for o in out])
    sp = np.hypot(seg_pts[:, 0], seg_pts[:, 1])
    st["point_spacing_m"] = {"p5": round(float(np.percentile(sp, 5)), 1), "median": round(float(np.median(sp)), 1),
                             "p95": round(float(np.percentile(sp, 95)), 1), "max": round(float(sp.max()), 1)}
    st["record_length_m"] = {"median": round(float(np.median(length)), 0), "p95": round(float(np.percentile(length, 95)), 0),
                             "max": round(float(length.max()), 0)}

    # Connectivity: the network's own graph on shared endpoints, per landmass group.
    key = {}
    ends = []
    for o in out:
        for p in (o["pts"][0], o["pts"][-1]):
            k = (float(p[0]), float(p[1]), float(p[2]))
            ends.append(key.setdefault(k, len(key)))
    ends = np.array(ends).reshape(-1, 2)
    g = csr_matrix((np.ones(len(ends)), (ends[:, 0], ends[:, 1])), shape=(len(key), len(key)))
    _, comp = connected_components(g, directed=False)
    rc = comp[ends[:, 0]]
    deg = np.bincount(ends.ravel(), minlength=len(key))
    st["nodes"] = {"total": len(key), "dead_ends": int((deg == 1).sum()), "junctions_3plus": int((deg >= 3).sum())}
    # Fuzzy check like the loader's own node count: ends within 10 m (3-D) that are not identical.
    allp = np.array(list(key.keys()))
    near = cKDTree(allp).query_pairs(10.0)
    st["nodes"]["distinct_ends_within_10m"] = len(near)
    def grp(nd):
        return int(node_group[nd]) if nd < len(node_group) else -1

    rg = np.array([grp(r["u"]) if grp(r["u"]) >= 0 else grp(r["v"]) for r in recs])
    # Records with both ends over the sea (bridges, causeways) count with the landmass their part joins.
    for c_ in np.unique(rc[rg < 0]):
        m = (rc == c_) & (rg >= 0)
        if m.any():
            vals, w_ = np.unique(rg[m], return_inverse=True)
            rg[(rc == c_) & (rg < 0)] = vals[np.bincount(w_, length[m]).argmax()]
    names = {}
    # Landmass labels for the stats: map.json "landmasses" {name: [lat, lon]}; Taiwan's are the default there only.
    marks = geo.cfg.get("landmasses") or ({"Taiwan": (25.033, 121.565), "mainland (Fujian)": (26.074, 119.296),
                                           "Matsu Nangan": (26.160, 119.950)} if geo.cfg["mapId"] == "taiwanstrait" else {})
    for name, (lat, lon) in marks.items():
        x, z = geo.game_xz(lat, lon)
        i = int(np.argmin([(r["x"][0] - x) ** 2 + (r["z"][0] - z) ** 2 for r in recs]))
        names[int(rg[i])] = name
    conn = {}
    for gv in np.unique(rg):
        m = rg == gv
        tot = length[m].sum()
        per = np.bincount(rc[m], length[m])
        nm = names.get(int(gv), "sea" if gv < 0 else f"island {int(gv)}")
        if nm in conn:
            nm = f"{nm} {int(gv)}"
        conn[nm] = {"km": round(tot / 1000, 1), "largest_share": round(float(per.max() / tot), 4),
                    "components": int((per > 0).sum())}
    big = {k: v for k, v in sorted(conn.items(), key=lambda kv: -kv[1]["km"])}
    st["connectivity"] = {k: v for k, v in list(big.items())[:8]}
    st["connectivity_other_islands"] = {"groups": max(0, len(big) - 8),
                                        "km": round(sum(v["km"] for v in list(big.values())[8:]), 1)}
    lc = np.bincount(rc, length)
    st["largest_component_share_all"] = round(float(lc.max() / length.sum()), 4)

    # Grades on the written points.
    rows = []
    for o, r in zip(out, recs):
        p = o["pts"].astype(np.float64)
        ds = np.hypot(np.diff(p[:, 0]), np.diff(p[:, 2]))
        gr = np.abs(np.diff(p[:, 1])) / np.maximum(ds, 1e-6)
        m = ds >= 10
        rows.append((r["kind"], r["gcl"], ds[m], gr[m], p[:-1][m]))
    grades = {}
    worst = []
    for kname, kk in (("ground", GROUND), ("bridge", BRIDGE), ("tunnel", TUNNEL)):
        for c in range(4):
            sel = [(ds, gr) for k_, c_, ds, gr, _ in rows if k_ == kk and c_ == c]
            if not sel:
                continue
            ds = np.concatenate([a for a, _ in sel])
            gr = np.concatenate([b for _, b in sel])
            lim = REPORT_GRADE[c]
            grades[f"{kname}/{GAME_NAMES[c]}"] = {
                "limit": float(lim), "km_over_limit": round(ds[gr > lim].sum() / 1000, 2),
                "share_over_limit": round(float(ds[gr > lim].sum() / max(ds.sum(), 1)), 4),
                "p99": round(float(np.percentile(gr, 99)), 3) if len(gr) else 0, "max": round(float(gr.max()), 3) if len(gr) else 0}
    for k_, c_, ds, gr, p in rows:
        if k_ == GROUND and len(gr):
            i = int(np.argmax(gr - REPORT_GRADE[c_]))
            worst.append((float(gr[i]), GAME_NAMES[c_], round(float(p[i, 0])), round(float(p[i, 2])), round(float(ds[i]))))
    worst.sort(reverse=True)
    st["grades"] = grades
    st["worst_ground_grades"] = [dict(grade=round(g_, 3), cls=c_, x=x_, z=z_, over_m=d_) for g_, c_, x_, z_, d_ in worst[:12]]
    gm = kinds == GROUND
    cut_km, fill_km, cuts = 0.0, 0.0, []
    for r in recs:
        if r["kind"] != GROUND:
            continue
        dev = r["y"] - r["t"]
        ds = np.r_[np.diff(r["s"]), 0.0]
        cut_km += ds[dev < -BIG_CUT].sum() / 1000
        fill_km += ds[dev > LIFT_LIMIT - 0.05].sum() / 1000
        if dev.min() < -BIG_CUT:
            i = int(np.argmin(dev))
            cuts.append((float(-dev[i]), GAME_NAMES[r["gcl"]], round(float(r["x"][i])), round(float(r["z"][i]))))
    cuts.sort(reverse=True)
    st["ground_profile"] = {"lift_limit_m": LIFT_LIMIT, "cut_limit_m": CUT_LIMIT,
                            "max_lift_m": round(float(prof[gm, 2].max()), 2), "max_cut_m": round(float(prof[gm, 3].max()), 2),
                            "km_with_big_cut": round(cut_km, 1), "big_cut_m": BIG_CUT, "records_with_big_cut": len(cuts),
                            "km_at_fill_limit": round(fill_km, 1),
                            "deepest_cuts": [dict(cut_m=round(a, 1), cls=b, x=c, z=d) for a, b, c, d in cuts[:10]]}
    tun = [r for r in recs if r["kind"] == TUNNEL]
    st["tunnel_bores"] = {"count": len(tun), "bore_above_ground_by_2m": int(sum(1 for r in tun if (r["y"] - r["t"]).max() > 2.0)),
                          "deepest_cover_m": round(float(max(((r["t"] - r["y"]).max() for r in tun), default=0)), 0)}
    for where in ("water", "land"):
        rows_ = sorted([s_ for s_ in short if s_[0] == where], key=lambda s_: -s_[1])
        st[f"bridges_short_of_clearance_over_{where}"] = {
            "count": len(rows_), "note": "ramps from ends that sit on the ground cannot climb further within the grade",
            "worst": [dict(short_m=round(a, 1), length_m=round(b), x=round(c), z=round(d)) for _, a, b, c, d in rows_[:8]]}
    st["selection"] = report
    st["credit"] = CREDIT
    return st


if __name__ == "__main__":
    main(*sys.argv[1:])
