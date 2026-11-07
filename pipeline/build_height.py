"""Build the map's height grid from Copernicus GLO-30 tiles.

Output (in the map's build folder):
    heights.npy   float32, samples x samples, metres above sea level (sea floor negative),
                  row 0 = north edge, column 0 = west edge, one sample per terrain vertex
    landmask.npy  bool, True on dry land
    preview.png   2048 px shaded relief with the must-include places marked

Sea depth is synthetic for now (deepens with distance from the coast, capped at 70 m, which
is about the strait's real mean depth); GEBCO bathymetry replaces it later.
"""
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from scipy import ndimage

from mapgeo import MapGeo

SEA_MAX_DEPTH = 70.0
SEA_DEPTH_PER_KM = 4.0
SHORE_MIN_HEIGHT = 0.6
SHORE_MIN_DEPTH = 1.5
SEA_FLOOR_CAP = 600.0


def resample_dem(geo, dem_dir):
    n = geo.samples
    pitch_ground = geo.cfg["vertexPitchMetres"] * geo.scale
    half = (n - 1) * pitch_ground / 2
    transform = from_origin(-half - pitch_ground / 2, half + pitch_ground / 2, pitch_ground, pitch_ground)
    out = np.full((n, n), np.nan, dtype=np.float32)
    for tif in sorted(Path(dem_dir).glob("*.tif")):
        with rasterio.open(tif) as src:
            part = np.full((n, n), np.nan, dtype=np.float32)
            reproject(
                source=rasterio.band(src, 1),
                destination=part,
                dst_transform=transform,
                dst_crs=geo.crs(),
                src_nodata=src.nodata,
                dst_nodata=np.nan,
                resampling=Resampling.average,
            )
            fill = np.isnan(out) & ~np.isnan(part)
            out[fill] = part[fill]
    return out, pitch_ground


def shape_sea(h, pitch_ground, bathy=None, real_shallows=False, shore=None):
    """Sea = at or below 0 and connected to open water at the map edge (no tiles there = sea).

    Depth comes from the bathymetry grid when there is one (real sea floor), kept at least
    SHORE_MIN_DEPTH deep right off the coast so the shoreline stays where the DEM puts it,
    ramped in over the first kilometre, and capped at SEA_FLOOR_CAP. Without one it is
    synthetic: deepening with distance from the coast."""
    # float32 throughout: a 5101-sample grid is 104 MB per float32 array and the Deck has little memory to spare
    # (float64 temporaries peaked at ~1.6 GB and ran Norway out of memory).
    h = np.where(np.isnan(h), np.float32(0.0), h).astype(np.float32)
    wet = h <= 0.0
    labels, _ = ndimage.label(wet)
    edge_labels = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    sea = np.isin(labels, edge_labels[edge_labels > 0])
    del labels, wet
    land = ~sea
    dist_km = ndimage.distance_transform_edt(sea).astype(np.float32)
    dist_km *= np.float32(pitch_ground / 1000.0)
    # Shore profile: 1 m at the coast, deepening per km offshore. map.json can steepen it ("shoreSlope" m per km,
    # "shoreMaxDepth" m) and, with "shoreProfileIsMinimum", put it under the real depths too: for narrow sounds
    # and inlets no survey grid resolves (Falkland Sound, San Carlos Water read 1-5 m in every free dataset).
    shore = shore or {}
    per_km = np.float32(shore.get("shoreSlope", SEA_DEPTH_PER_KM))
    synthetic = np.minimum(np.float32(1.0) + dist_km * per_km, np.float32(shore.get("shoreMaxDepth", SEA_MAX_DEPTH)))
    if bathy is not None:
        real = fill_bathy_gaps(-np.asarray(bathy, dtype=np.float32), sea, synthetic, dist_km, real_shallows)
        np.clip(real, SHORE_MIN_DEPTH, SEA_FLOOR_CAP, out=real)
        ramp = np.clip(dist_km, 0, 1)            # blend from the synthetic shore profile over the first kilometre
        depth = np.maximum(np.float32(SHORE_MIN_DEPTH), synthetic * (1 - ramp) + real * ramp)
        if shore.get("shoreProfileIsMinimum"):
            depth = np.maximum(depth, synthetic)
        del real, ramp
    else:
        depth = synthetic
    h = np.where(sea, -depth, np.maximum(h, np.float32(SHORE_MIN_HEIGHT)))
    return h.astype(np.float32), land


BATHY_NODATA_DEPTH = 2.0  # metres: open water shallower than this, away from the shore, is the source's "no data"
BATHY_NODATA_OFFSHORE_KM = 0.5
GAP_FILL_STEP = 4         # gap filling works on a grid 4x coarser (5101 -> 1276 samples) to keep memory small


def fill_bathy_gaps(depth, sea, synthetic, dist_km, real_shallows=False):
    """Terrarium sea-floor tiles have no soundings in many coastal waters and hold 0 there (41 % of Norway's
    grid) or values around -1 m (Falkland Sound, which held warships in 1982). Read literally that makes fjords
    and sounds 1-2 m deep and keeps ships out. Open water shallower than BATHY_NODATA_DEPTH more than
    BATHY_NODATA_OFFSHORE_KM from the shore takes the depth of the nearest real sounding, and never less than
    the synthetic shore profile (4 m per km from the coast). Maps with real wide tidal flats (Korea, the
    Changhua coast) set "realShallows": true in map.json, which limits this to exact zeros.
    Then the whole field gets a light blur so the source's 1 km cells and dots do not print as a grid."""
    if real_shallows:
        missing = sea & (np.abs(depth) < 0.5)
    else:
        missing = sea & (depth < BATHY_NODATA_DEPTH) & (dist_km > BATHY_NODATA_OFFSHORE_KM)
    valid = sea & (depth >= BATHY_NODATA_DEPTH)
    share = missing.sum() / max(1, sea.sum())
    if share > 0.005 and valid.any():
        s = GAP_FILL_STEP
        v_small = valid[::s, ::s]
        if v_small.any():
            # Nearest valid coarse cell for every coarse cell, then read back at full resolution.
            iy, ix = ndimage.distance_transform_edt(~v_small, return_distances=False, return_indices=True)
            near = depth[::s, ::s][iy, ix].astype(np.float32)
            del iy, ix
            near_full = np.repeat(np.repeat(near, s, axis=0), s, axis=1)[: depth.shape[0], : depth.shape[1]]
            depth = np.where(missing, np.maximum(near_full, synthetic), depth).astype(np.float32)
            del near, near_full
            print(f"sea floor: {share:.0%} of the sea had no soundings; filled from the nearest real depth")
    depth = ndimage.gaussian_filter(depth, sigma=1.5).astype(np.float32)
    return depth


def fade_edges(h, land, geo):
    """Slope the outer band down into the sea so the map ends in water, not a cliff."""
    n = h.shape[0]
    pitch = geo.cfg["vertexPitchMetres"]
    band = int(geo.cfg.get("edgeFadeMetres", 9000) / pitch)
    idx = np.arange(n)
    d = np.minimum(idx, n - 1 - idx)
    edge = np.minimum.outer(d, d)
    t = np.clip(edge / band, 0, 1)
    t = t * t * (3 - 2 * t)
    target = -SEA_MAX_DEPTH
    out = target + (h - target) * t
    land = land & (out > 0)
    return out.astype(np.float32), land


def preview(h, land, geo, path, size=2048):
    step = max(1, h.shape[0] // size)
    hs = h[::step, ::step]
    ls = land[::step, ::step]
    pitch = geo.cfg["vertexPitchMetres"] * step
    dy, dx = np.gradient(hs, pitch)
    slope = np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    az, alt = np.radians(315), np.radians(40)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    shade = np.clip(shade, 0, 1)

    t = np.clip(hs / 3500.0, 0, 1)[..., None]
    low = np.array([96, 140, 80]) / 255
    mid = np.array([170, 150, 100]) / 255
    high = np.array([245, 245, 245]) / 255
    col = np.where(t < 0.4, low + (mid - low) * (t / 0.4), mid + (high - mid) * ((t - 0.4) / 0.6))
    col = col * (0.45 + 0.55 * shade[..., None])
    depth = np.clip(-hs / SEA_MAX_DEPTH, 0, 1)[..., None]
    sea = (np.array([120, 180, 215]) + (np.array([30, 70, 130]) - np.array([120, 180, 215])) * depth) / 255
    rgb = np.where(ls[..., None], col, sea)
    img = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))

    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    w = img.width
    half = geo.size_m / 2
    for name, (lat, lon) in geo.cfg["mustInclude"].items():
        x, z = geo.game_xz(lat, lon)
        px = (x + half) / geo.size_m * w
        py = (half - z) / geo.size_m * w
        d.ellipse([px - 7, py - 7, px + 7, py + 7], outline=(200, 16, 46), width=4)
        d.text((px + 12, py - 14), name, fill=(20, 20, 20), font=font, stroke_width=3, stroke_fill=(255, 255, 255))
    km = 50_000 / geo.size_m * w
    d.rectangle([40, w - 60, 40 + km, w - 48], fill=(20, 20, 20))
    d.text((40, w - 100), "50 km", fill=(20, 20, 20), font=font, stroke_width=3, stroke_fill=(255, 255, 255))
    img.save(path)


def main(cfg_path, dem_dir, out_dir):
    geo = MapGeo.load(cfg_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw, pitch_ground = resample_dem(geo, dem_dir)
    bathy_path = out / "bathy.npy"
    bathy = np.load(bathy_path) if bathy_path.exists() else None
    h, land = shape_sea(raw, pitch_ground, bathy, bool(geo.cfg.get("realShallows", False)), geo.cfg)
    h, land = fade_edges(h, land, geo)
    np.save(out / "heights.npy", h)
    np.save(out / "landmask.npy", land)
    preview(h, land, geo, out / "preview.png")
    print(f"{h.shape[0]} x {h.shape[1]} samples, land {land.mean()*100:.1f} %, "
          f"highest {h.max():.0f} m, deepest {h.min():.0f} m")


if __name__ == "__main__":
    main(*sys.argv[1:])
