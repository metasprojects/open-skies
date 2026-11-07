"""Small checks the build runs between pipeline stages, each in its own short-lived process so the app itself
stays light. Prints one JSON object.

    stagehelpers.py count <overture dir>                     -> {"roads": n, "buildings": n, ...}
    stagehelpers.py cover <map.json> <worldcover dir>        -> {"land": share, "desert": share, ...}
    stagehelpers.py heights <build dir>                      -> {"max": m, "min": m, "land": share}
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


def count(ov):
    import duckdb
    c = duckdb.connect(config={"memory_limit": "300MB", "threads": 1})
    out = {}
    for f in sorted(Path(ov).glob("*.parquet")):
        try:
            out[f.stem] = c.execute(f"SELECT count(*) FROM read_parquet('{f.as_posix()}')").fetchone()[0]
        except Exception as e:  # noqa: BLE001
            out[f.stem] = -1
            out.setdefault("errors", []).append(f"{f.name}: {e}"[:200])
    return out


def cover(cfg, wc):
    """Share of the map's land in each WorldCover class, read coarsely (1 km), to choose the ground style."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import Resampling

    from mapgeo import MapGeo
    geo = MapGeo.load(cfg)
    size = 256
    half = geo.ground_size_m / 2
    px = geo.ground_size_m / size
    dst = from_origin(-half, half, px, px)
    lc = np.zeros((size, size), np.uint8)
    for tif in sorted(Path(wc).glob("*.tif")):
        with rasterio.open(tif) as src, WarpedVRT(src, crs=geo.crs(), transform=dst, width=size, height=size,
                                                  resampling=Resampling.mode, nodata=0) as vrt:
            a = vrt.read(1)
            lc = np.where(lc == 0, a, lc)
    land = (lc != 0) & (lc != 80)
    n = max(int(land.sum()), 1)
    share = {int(k): float(((lc == k) & land).sum() / n) for k in (10, 20, 30, 40, 50, 60, 70, 90, 95, 100)}
    return {"land": float(land.mean()), "bare": share[60], "shrub": share[20], "snow": share[70],
            "built": share[50], "tree": share[10], "desert": share[60] + 0.5 * share[20]}


def heights(build):
    import numpy as np
    h = np.load(Path(build) / "heights.npy", mmap_mode="r")
    s = h[::8, ::8]
    return {"max": float(s.max()), "min": float(s.min()), "land": float((s > 0).mean())}


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    print(json.dumps({"count": count, "cover": cover, "heights": heights}[cmd](*args)))
