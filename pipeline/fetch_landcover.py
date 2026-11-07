"""Download ESA WorldCover 10 m (2021, v200) tiles and OurAirports CSVs for a map.

WorldCover: CC BY 4.0, AWS Registry of Open Data, 3 x 3 degree tiles named by their SW corner.
OurAirports: public domain.
"""
import math
import sys
from pathlib import Path

import requests

from mapgeo import MapGeo

WC = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"
OA = "https://davidmegginson.github.io/ourairports-data"


def get(url, dest):
    if dest.exists() and dest.stat().st_size > 0:
        return "cached"
    r = requests.get(url, timeout=300, stream=True)
    if r.status_code == 404:
        return "none"
    r.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    tmp.replace(dest)
    return f"{dest.stat().st_size/1e6:.1f} MB"


def main(cfg_path, data_dir):
    geo = MapGeo.load(cfg_path)
    w, s, e, n = geo.geo_bounds(margin_m=5000)
    wc = Path(data_dir) / "esa-worldcover"
    oa = Path(data_dir) / "ourairports"
    wc.mkdir(parents=True, exist_ok=True)
    oa.mkdir(parents=True, exist_ok=True)
    for lat in range(math.floor(s / 3) * 3, math.floor(n / 3) * 3 + 1, 3):
        for lon in range(math.floor(w / 3) * 3, math.floor(e / 3) * 3 + 1, 3):
            name = f"ESA_WorldCover_10m_2021_v200_{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}_Map.tif"
            print(f"  {name}: {get(f'{WC}/{name}', wc / name)}")
    for name in ("airports.csv", "runways.csv"):
        print(f"  {name}: {get(f'{OA}/{name}', oa / name)}")


if __name__ == "__main__":
    main(*sys.argv[1:])
