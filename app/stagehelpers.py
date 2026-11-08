"""Small checks the build runs between pipeline stages, each in its own short-lived process so the app itself
stays light. Prints one JSON object.

    stagehelpers.py count <overture dir>                     -> {"roads": n, "buildings": n, ...}
    stagehelpers.py cover <map.json> <worldcover dir>        -> {"land": share, "desert": share, ...}
    stagehelpers.py heights <build dir>                      -> {"max": m, "min": m, "land": share}
    stagehelpers.py apron <map.json> <build dir>             -> {base: apron side}
    stagehelpers.py idents <map.json> <build dir> <ourairports dir> -> {base: "05/23 ..."}
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


def apron(cfg, build):
    """Which side of each runway its apron goes on (airbases.json "apronSide"), decided by the pipeline's own
    apron_side.side_for. Its command line wants another folder layout, so Open Skies calls it with its own."""
    import apron_side
    from mapgeo import MapGeo
    from placement import MapGround
    geo = MapGeo.load(cfg)
    g = MapGround(build, geo.cfg)
    path = Path(build) / "airbases.json"
    bases = json.loads(path.read_text(encoding="utf-8"))
    for b in bases:
        b["apronSide"] = apron_side.side_for(g, b)
    path.write_text(json.dumps(bases, indent=2), encoding="utf-8")
    return {b["uniqueName"]: b["apronSide"] for b in bases}


def idents(cfg, build, oa):
    """Each runway's real designators (airbases.json "leIdent"/"heIdent"), for the runway numbers MapBuilder paints,
    by the pipeline's own runway_idents. It reads <root>/maps/<map>/map.json and <root>/build/<map>/airbases.json,
    the same layout as Open Skies' data folder, so it only needs pointing at that folder and its OurAirports copy."""
    import runway_idents
    cfg, build = Path(cfg).resolve(), Path(build).resolve()
    root = build.parent.parent
    if cfg != (root / "maps" / build.name / "map.json").resolve():
        raise SystemExit(f"unexpected layout: {cfg} vs {build}")
    runway_idents.ROOT, runway_idents.OA = root, Path(oa)
    runway_idents.main(build.name)
    bases = json.loads((build / "airbases.json").read_text(encoding="utf-8"))
    return {b["uniqueName"]: " ".join(f"{r.get('leIdent') or '?'}/{r.get('heIdent') or '?'}"
                                      for r in [b] + list(b.get("extraRunways") or [])) for b in bases}


def heights(build):
    import numpy as np
    h = np.load(Path(build) / "heights.npy", mmap_mode="r")
    s = h[::8, ::8]
    return {"max": float(s.max()), "min": float(s.min()), "land": float((s > 0).mean())}


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    print(json.dumps({"count": count, "cover": cover, "heights": heights, "apron": apron,
                      "idents": idents}[cmd](*args)))
