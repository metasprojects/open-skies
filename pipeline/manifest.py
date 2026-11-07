"""Write the map's NOCustomMaps manifest (the bundle's "map" TextAsset) into the build folder.

Field meanings and rules: NOCustomMaps Core/MapManifest.cs (MIT). The grid follows its
DefaultGrid rule; the network prefix follows MapIdentity.AllocatePrefix (FNV-1a 64 over UTF-16).
"""
import json
import math
import sys
from pathlib import Path

from mapgeo import MapGeo

CREDITS = ("produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH "
           "2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved. "
           "Land cover © ESA WorldCover project 2021 (CC BY 4.0). Roads and places © OpenStreetMap contributors, "
           "Overture Maps Foundation (ODbL). Ship traffic: World Bank/IMF (CC BY 4.0). Airports: OurAirports.")
NOTICE = ("The organisations in charge of the Copernicus programme by law or by delegation do not incur any liability "
          "for any use of the Copernicus WorldDEM-30. Airbus Defence and Space, DLR, ESA, the European Union and "
          "Copernicus do not endorse this map, its author or any use made of it. Anyone who receives this map, or "
          "anything made from it that still holds data derived from the Copernicus DEM, is bound by the obligations "
          "of the Copernicus DEM licence: to give its modified-data notice and the liability sentence above when they "
          "pass it on or show it to the public, and not to give the public the impression that their activities are "
          "officially endorsed by Airbus Defence and Space, the licensor or anyone else in charge of Copernicus. "
          "Land cover: ESA WorldCover 10 m 2021 v200, © ESA WorldCover project / contains modified Copernicus "
          "Sentinel data (2021) processed by the ESA WorldCover consortium, licensed CC BY 4.0. Airport positions: "
          "OurAirports, public domain. This is an unofficial fan map, not made or endorsed by Shockfront Studios.")


def fnv1a64(s):
    h = 14695981039346656037
    for ch in s:
        c = ord(ch)
        for byte in (c & 0xFF, (c >> 8) & 0xFF):
            h = ((h ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def default_grid(size):
    half = size / 2
    offset = int(math.ceil(half / 10000) * 10000) + 20000
    cells = int(math.ceil((offset + half) / 10000))
    return (cells + 3) // 4 * 4, offset


def main(cfg_path, build_dir):
    geo = MapGeo.load(cfg_path)
    c = geo.cfg
    mid = c["mapId"]
    size = geo.size_m
    grid, offset = default_grid(size)
    prefix = 16 + fnv1a64(mid) % 96
    root = "".join(w.capitalize() for w in c["displayName"].split())
    credits = CREDITS + (" Sea floor: NOAA NCEI ETOPO 2022." if c.get("seaFloor") == "etopo2022"
                         else " Sea floor: AWS Terrain Tiles (GEBCO, NOAA, others).")
    m = {
        "mapId": mid, "displayName": c["displayName"], "version": c["version"], "author": c["author"],
        "tagColor": c.get("tagColor", ""), "builtAgainstGameHash": c.get("builtAgainstGameHash", ""),
        "credits": credits, "notice": NOTICE,
        "//source": f"Copernicus GLO-30 + ESA WorldCover 2021, centre {geo.lat0}N {geo.lon0}E, "
                    f"{geo.ground_size_m/1000:.2f} km of ground at {geo.scale}x",
        "mapSizeX": size, "mapSizeY": size, "gridSizeX": grid, "gridSizeY": grid, "offsetX": offset, "offsetY": offset,
        "latitude": geo.lat0, "terrainHeightMax": c.get("terrainHeightMax", 4500), "mapPrefix": prefix,
        "assets": {
            "rootPrefab": root, "terrainRoot": "Terrain",
            "oceanBasecolor": f"{mid}_ocean_basecolor", "oceanDepthmap": f"{mid}_ocean_depth",
            "terrainColorMap": f"{mid}_color", "terrainMacroColor": f"{mid}_macro",
            "splatGrass": f"{mid}_splat_grass", "splatRock": f"{mid}_splat_rock",
            "splatLush": f"{mid}_splat_lush", "splatFields": f"{mid}_splat_fields",
            "airbases": f"{mid}_airbases", "mapImage": f"{mid}_map",
        },
        "lakes": [],
        "camera": c.get("camera", {"x": -20000, "y": 3000, "z": 20000, "pitch": 8, "yaw": 110, "roll": 0}),
    }
    # Optional layers: present when their pipeline stage has run.
    for key, stem in (("seaLanes", "sealanes"), ("roadNetwork", "roads")):
        if (Path(build_dir) / f"{stem}.bytes").exists():
            m["assets"][key] = f"{mid}_{stem}"
    assert len(credits) <= 440, len(credits)
    Path(build_dir, "manifest.json").write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"manifest: {size} m, grid {grid} offset {offset}, prefix {prefix}, root prefab {root}, credits {len(credits)} chars")


if __name__ == "__main__":
    main(*sys.argv[1:])
