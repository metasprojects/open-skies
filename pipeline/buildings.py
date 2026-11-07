"""Turn Overture buildings + land use into a compact, zoned building table for the map.

Output (build folder):
    buildings.npz  x, z (game metres), area (m2), long, short (m, of the min rotated rectangle),
                   yaw (deg), height (m, real where known else estimated), height_real (bool),
                   zone (zoning.py), name_idx/names (named buildings only)
    zoning.png     whole-map zoning picture (4096), zoning_<place>.png close-ups
Zone priority: building tag > land-use area > WorldCover > density (see zoning.py).
Height priority: Overture height > floors x 3.2 m > typical height for its zone.
"""
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw
from rasterio.features import rasterize
from rasterio.transform import from_origin
from scipy.spatial import cKDTree
from shapely import from_wkb

import zoning as Z
from mapgeo import MapGeo
from overture import connect

TYPICAL_HEIGHT = {Z.RESIDENTIAL: 9.0, Z.COMMERCIAL: 18.0, Z.INDUSTRIAL: 10.0, Z.AGRICULTURAL: 5.0, Z.MILITARY: 7.0,
                  Z.PORT: 12.0, Z.CIVIC: 12.0, Z.AIRPORT: 14.0, Z.UNKNOWN: 7.0}


def proj4(geo):
    return geo.crs().to_proj4()


def load_buildings(c, geo, src):
    p = proj4(geo)
    q = f"""
        WITH b AS (
          SELECT ST_Transform(geometry, 'EPSG:4326', '{p}', true) AS g, height, num_floors,
                 class, subtype, name
          FROM read_parquet('{src.as_posix()}')
          WHERE coalesce(is_underground, false) = false
        ), r AS (
          SELECT g, height, num_floors, class, subtype, name,
                 ST_ExteriorRing(ST_MinimumRotatedRectangle(g)) AS ring
          FROM b WHERE ST_Area(g) >= 12
        )
        SELECT ST_X(ST_Centroid(g))::FLOAT AS x, ST_Y(ST_Centroid(g))::FLOAT AS y, ST_Area(g)::FLOAT AS area,
               (ST_X(ST_PointN(ring, 2)) - ST_X(ST_PointN(ring, 1)))::FLOAT AS e1x,
               (ST_Y(ST_PointN(ring, 2)) - ST_Y(ST_PointN(ring, 1)))::FLOAT AS e1y,
               (ST_X(ST_PointN(ring, 3)) - ST_X(ST_PointN(ring, 2)))::FLOAT AS e2x,
               (ST_Y(ST_PointN(ring, 3)) - ST_Y(ST_PointN(ring, 2)))::FLOAT AS e2y,
               height::FLOAT AS height, num_floors::FLOAT AS num_floors, class, subtype, name
        FROM r
    """
    return c.execute(q).fetchnumpy()


def rect_dims(d):
    l1 = np.hypot(d["e1x"], d["e1y"])
    l2 = np.hypot(d["e2x"], d["e2y"])
    first = l1 >= l2
    long_ = np.where(first, l1, l2).astype(np.float32)
    short_ = np.where(first, l2, l1).astype(np.float32)
    yaw = np.where(first, np.degrees(np.arctan2(d["e1x"], d["e1y"])), np.degrees(np.arctan2(d["e2x"], d["e2y"]))) % 180
    return np.nan_to_num(long_), np.nan_to_num(short_), np.nan_to_num(yaw).astype(np.float32)


def land_use_raster(c, geo, src, size):
    p = proj4(geo)
    rows = c.execute(f"""
        SELECT subtype, class, ST_AsWKB(ST_Transform(geometry, 'EPSG:4326', '{p}', true)) AS g
        FROM read_parquet('{src.as_posix()}')
        WHERE ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')
    """).fetchall()
    half = geo.ground_size_m / 2
    px = geo.ground_size_m / size
    transform = from_origin(-half, half, px, px)
    shapes = {}
    for subtype, cls, wkb in rows:
        zone = Z.land_use_zone(subtype, cls)
        if zone is not None:
            shapes.setdefault(zone, []).append(from_wkb(wkb))
    grid = np.full((size, size), 255, np.uint8)
    for zone in Z.LAND_USE_PRIORITY:
        if zone in shapes:
            layer = rasterize(((g, 1) for g in shapes[zone]), out_shape=(size, size), transform=transform, fill=0,
                              dtype=np.uint8)
            grid[layer == 1] = zone
    return grid, transform


def cover_at(geo, wc_dir, xs, ys):
    out = np.zeros(len(xs), np.uint8)
    t = geo.to_geo()
    lon, lat = t.transform(xs, ys)
    for tif in sorted(Path(wc_dir).glob("*.tif")):
        with rasterio.open(tif) as src:
            b = src.bounds
            m = (lon >= b.left) & (lon < b.right) & (lat > b.bottom) & (lat <= b.top) & (out == 0)
            if m.any():
                out[m] = np.array([v[0] for v in src.sample(zip(lon[m], lat[m]))], np.uint8)
    return out


def picture(x, y, zone, height, half, path, size, crop=None):
    img = Image.new("RGB", (size, size), (18, 22, 28))
    d = ImageDraw.Draw(img)
    x0, y0, span = (-half, half, 2 * half) if crop is None else crop
    k = size / span
    sel = (x >= x0) & (x <= x0 + span) & (y <= y0) & (y >= y0 - span)
    order = np.argsort(height[sel])
    xs, ys, zs, hs = x[sel][order], y[sel][order], zone[sel][order], height[sel][order]
    r = max(1.0, 0.6 * size / 4096 * (2 * half / span) ** 0.5)
    for px_, py_, z_, h_ in zip((xs - x0) * k, (y0 - ys) * k, zs, hs):
        col = Z.COLOURS[z_]
        lift = min(1.0, 0.55 + h_ / 120)
        rr = r * (1 + min(h_, 200) / 60)
        d.ellipse([px_ - rr, py_ - rr, px_ + rr, py_ + rr], fill=tuple(int(cv * lift) for cv in col))
    y_ = 10
    for i, name in enumerate(Z.NAMES[:-1]):
        d.rectangle([10, y_, 30, y_ + 16], fill=Z.COLOURS[i]); d.text((36, y_), name, fill=(240, 240, 240)); y_ += 22
    img.save(path)


def main(cfg_path, overture_dir, wc_dir, build_dir):
    geo = MapGeo.load(cfg_path)
    ov, b = Path(overture_dir), Path(build_dir)
    c = connect()
    data = load_buildings(c, geo, ov / "buildings.parquet")
    x, y, area = data["x"].astype(np.float32), data["y"].astype(np.float32), data["area"].astype(np.float32)
    long_, short_, yaw = rect_dims(data)
    n = len(x)
    if n == 0:
        # No mapped buildings in the area: an empty table, so roads, placement and the Unity stage carry on.
        e32 = np.zeros(0, np.float32)
        np.savez_compressed(b / "buildings.npz", x=e32, z=e32, area=e32, long=e32, short=e32, yaw=e32, height=e32,
                            height_real=np.zeros(0, bool), zone=np.zeros(0, np.uint8), named=np.zeros(0, np.int64),
                            names=np.array([], dtype=object))
        print("no buildings in this area: wrote an empty buildings table")
        return

    def filled(a, fill):
        return np.ma.filled(a, fill) if np.ma.isMaskedArray(a) else np.asarray(a)

    zone = np.full(n, 255, np.uint8)
    for i, (cls, sub) in enumerate(zip(filled(data["class"], None), filled(data["subtype"], None))):
        zb = Z.building_zone(cls, sub)
        if zb is not None:
            zone[i] = zb
    tagged = (zone != 255).sum()

    # Real places inside buildings: majority vote per building, for buildings their own tag left open.
    p = proj4(geo)
    pl = c.execute(f"""
        SELECT ST_X(g)::FLOAT AS x, ST_Y(g)::FLOAT AS y, basic_category FROM (
          SELECT ST_Transform(geometry, 'EPSG:4326', '{p}', true) AS g, basic_category
          FROM read_parquet('{(ov / 'places.parquet').as_posix()}')
          WHERE coalesce(operating_status, 'open') <> 'permanently_closed')
    """).fetchnumpy()
    tree = cKDTree(np.column_stack([x, y]))
    dist, idx = tree.query(np.column_stack([pl["x"], pl["y"]]), k=4, distance_upper_bound=80)
    reach = np.sqrt(area / np.pi) + 12.0
    votes = np.zeros((n, len(Z.NAMES)), np.uint16)
    for k, cat in enumerate(filled(pl["basic_category"], None)):
        zp = Z.place_zone(cat)
        if zp is None:
            continue
        for d_, j in zip(dist[k], idx[k]):
            if j < n and d_ <= reach[j]:
                votes[j, zp] += 1
                break
    by_place = (zone == 255) & (votes.sum(1) > 0)
    zone[by_place] = votes[by_place].argmax(1).astype(np.uint8)

    lu, tr = land_use_raster(c, geo, ov / "land_use.parquet", 8192)
    col = ((x - tr.c) / tr.a).astype(int).clip(0, 8191)
    row = ((y - tr.f) / tr.e).astype(int).clip(0, 8191)
    from_lu = (zone == 255) & (lu[row, col] != 255)
    zone[from_lu] = lu[row, col][from_lu]

    rest = zone == 255
    wc = cover_at(geo, wc_dir, x[rest].astype(float), y[rest].astype(float))
    zone[np.where(rest)[0]] = np.array([Z.WORLDCOVER.get(int(v), Z.RESIDENTIAL) for v in wc], np.uint8)

    h = filled(np.ma.asarray(data["height"]).astype(np.float64), np.nan)
    floors = filled(np.ma.asarray(data["num_floors"]).astype(np.float64), np.nan)
    h = np.where(h > 0, h, np.nan)
    h = np.where(~np.isnan(h), h, np.where(floors > 0, floors * 3.2, np.nan))
    known = ~np.isnan(h)
    typical = np.array([TYPICAL_HEIGHT[int(zz)] for zz in zone])
    h = np.where(known, h, typical).astype(np.float32)

    names = [str(v) if v is not None else "" for v in filled(data["name"], None)]
    gx, gz = x / geo.scale, y / geo.scale
    # Drop buildings in the outer fade band, where the terrain is sloped down into the sea on purpose, and any
    # left standing in water: Hormuz kept 168k there, including 217 towers of 100 m+ in Dubai and Sharjah.
    hm = np.load(b / "heights.npy", mmap_mode="r")
    half, pitch = geo.size_m / 2, geo.cfg["vertexPitchMetres"]
    ci = ((gx + half) / pitch).round().astype(np.int64).clip(0, hm.shape[1] - 1)
    ri = ((half - gz) / pitch).round().astype(np.int64).clip(0, hm.shape[0] - 1)
    edge = half - np.maximum(np.abs(gx), np.abs(gz))
    keep = (edge > geo.cfg.get("edgeFadeMetres", 9000)) & (np.asarray(hm[ri, ci]) > 0.5)
    dropped = int((~keep).sum())
    del hm, ci, ri, edge
    if dropped:
        print(f"dropped {dropped:,} buildings in the edge fade band or in water")
        gx, gz, area, long_, short_, yaw, h, known, zone = (a[keep] for a in (gx, gz, area, long_, short_, yaw, h, known, zone))
        names = [s for s, k in zip(names, keep) if k]
        n = int(keep.sum())
    named = np.array([i for i, s in enumerate(names) if s], np.int64)
    np.savez_compressed(b / "buildings.npz", x=gx, z=gz, area=area, long=long_, short=short_, yaw=yaw, height=h,
                        height_real=known, zone=zone, named=named,
                        names=np.array([names[i] for i in named], dtype=object))

    stats = {Z.NAMES[k]: int((zone == k).sum()) for k in range(len(Z.NAMES)) if (zone == k).any()}
    print(f"{n:,} buildings; by own tag {tagged:,}, by places inside {by_place.sum():,}, by land use {from_lu.sum():,}, "
          f"by land cover {rest.sum():,}")
    if n:
        print(f"heights known for {known.sum():,} ({known.mean()*100:.0f}%); 100 m+: {(h >= 100).sum():,}; "
              f"tallest {h.max():.0f} m")
    print("zones:", json.dumps(stats))
    half = geo.size_m / 2
    picture(gx, gz, zone, h, half, b / "zoning.png", 4096)
    closeups = geo.cfg.get("closeups") or ({"taipei": [25.045, 121.53], "taichung": [24.16, 120.66], "fuzhou": [26.07, 119.30]}
                                           if geo.cfg["mapId"] == "taiwanstrait" else {})
    for name, (lat, lon) in closeups.items():
        cx, cz = geo.game_xz(lat, lon)
        span = 24000
        picture(gx, gz, zone, h, half, b / f"zoning_{name}.png", 2048, (cx - span / 2, cz + span / 2, span))


if __name__ == "__main__":
    main(*sys.argv[1:])
