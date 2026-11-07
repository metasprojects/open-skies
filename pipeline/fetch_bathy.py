"""Sea-floor depth for a map from the AWS open terrain tiles (Terrarium PNG encoding).

Source: s3://elevation-tiles-prod (AWS Registry of Open Data, public). Ocean depth there comes
from ETOPO1 / GEBCO / NOAA coastal models; attribution list:
https://github.com/tilezen/joerd/blob/master/docs/attribution.md
Terrarium decoding: metres = R * 256 + G + B / 256 - 32768.

Writes <out>/bathy.npy: float32 on the map's height grid (row 0 = north), metres, sea negative.
"""
import io
import math
import sys
from pathlib import Path

import numpy as np
import requests
from PIL import Image
from scipy import ndimage

from mapgeo import MapGeo

URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
ZOOM = 10


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def main(cfg_path, cache_dir, out_dir):
    geo = MapGeo.load(cfg_path)
    w, s, e, n = geo.geo_bounds(margin_m=3000)
    x0, y0 = lonlat_to_tile(w, n, ZOOM)
    x1, y1 = lonlat_to_tile(e, s, ZOOM)
    tx0, ty0, tx1, ty1 = int(x0), int(y0), int(x1), int(y1)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    mosaic = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), np.float32)
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            f = cache / f"{ZOOM}_{tx}_{ty}.png"
            if not f.exists():
                r = requests.get(URL.format(z=ZOOM, x=tx, y=ty), timeout=60)
                r.raise_for_status()
                f.write_bytes(r.content)
            a = np.asarray(Image.open(f).convert("RGB")).astype(np.float32)
            m = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
            mosaic[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = m

    # Sample the mosaic at every height-grid sample.
    size = geo.samples
    pitch_g = geo.cfg["vertexPitchMetres"] * geo.scale
    half = (size - 1) * pitch_g / 2
    xs = -half + np.arange(size) * pitch_g
    t = geo.to_geo()
    out = np.empty((size, size), np.float32)
    for r in range(size):
        lon, lat = t.transform(xs, np.full(size, half - r * pitch_g))
        fx, fy = lonlat_to_tile(np.asarray(lon), np.asarray(lat), ZOOM) if False else (None, None)
        nz = 2 ** ZOOM
        fx = (np.asarray(lon) + 180) / 360 * nz
        fy = (1 - np.arcsinh(np.tan(np.radians(np.asarray(lat)))) / np.pi) / 2 * nz
        px = (fx - tx0) * 256 - 0.5
        py = (fy - ty0) * 256 - 0.5
        out[r] = ndimage.map_coordinates(mosaic, [py, px], order=1, mode="nearest")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    np.save(Path(out_dir) / "bathy.npy", out)
    sea = out < 0
    print(f"{(tx1-tx0+1)*(ty1-ty0+1)} tiles; sea depth median {np.median(out[sea]):.0f} m, deepest {out.min():.0f} m")


if __name__ == "__main__":
    main(*sys.argv[1:])
