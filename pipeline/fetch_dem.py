"""Download the Copernicus GLO-30 DEM tiles that cover a map.

Source: Copernicus DEM GLO-30 on the AWS Registry of Open Data (public, no account).
Tiles that are all ocean do not exist in the bucket; a 404 for one of those is expected.
"""
import math
import sys
from pathlib import Path

import requests

from mapgeo import MapGeo

BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"


def tile_name(lat, lon):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def main(cfg_path, out_dir):
    geo = MapGeo.load(cfg_path)
    w, s, e, n = geo.geo_bounds(margin_m=5000)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    got = missing = 0
    for lat in range(math.floor(s), math.floor(n) + 1):
        for lon in range(math.floor(w), math.floor(e) + 1):
            name = tile_name(lat, lon)
            dest = out / f"{name}.tif"
            if dest.exists() and dest.stat().st_size > 0:
                got += 1
                continue
            r = requests.get(f"{BUCKET}/{name}/{name}.tif", timeout=120, stream=True)
            if r.status_code == 404:
                missing += 1
                print(f"  {name}: none (all sea)")
                continue
            r.raise_for_status()
            tmp = dest.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            tmp.replace(dest)
            got += 1
            print(f"  {name}: {dest.stat().st_size/1e6:.1f} MB")
    print(f"{got} tiles present, {missing} all-sea")


if __name__ == "__main__":
    main(*sys.argv[1:])
