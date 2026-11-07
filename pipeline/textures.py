"""Make the map's textures from heights + ESA WorldCover, in the style the game's maps use.

Memory-light: the 4096 maps are made whole (uint8 / float32 layers of 64 MB), the 8192 ones in
strips of rows, so the whole run stays well under 1 GB.

Writes PNGs into the build folder (north at the top; Unity's importer puts the top row at v = 1,
which is +z, north):
    macro.png 8192, color.png 1024, splat_{grass,rock,lush,fields}.png 4096, grass_mask.png 4096,
    ocean_depth.png 2048, ocean_basecolor.png 2048, map.png 8192 (dark tactical chart)
WorldCover classes: 10 tree, 20 shrub, 30 grass, 40 crop, 50 built, 60 bare, 70 snow, 80 water,
90 wetland, 95 mangrove, 100 moss.
"""
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling
from rasterio.windows import Window
from scipy import ndimage

from mapgeo import MapGeo

Image.MAX_IMAGE_PIXELS = None
STRIP = 512
MARGIN = 32

LOW = np.array([118, 150, 82], np.float32)
HILL = np.array([132, 138, 84], np.float32)
MID = np.array([150, 128, 96], np.float32)
PEAK = np.array([228, 226, 222], np.float32)
FOREST = np.array([78, 110, 62], np.float32)
FIELD = np.array([160, 168, 96], np.float32)
TOWN = np.array([150, 148, 140], np.float32)
ROCKC = np.array([142, 130, 116], np.float32)
BARE = np.array([146, 140, 128], np.float32)      # bare ground on temperate maps: gravel, scree, riverbeds
SAND = np.array([200, 174, 132], np.float32)      # bare ground on desert maps
SCRUB = np.array([164, 150, 108], np.float32)     # arid shrubland
SHALLOW = np.array([150, 160, 130], np.float32)
DEEP = np.array([40, 70, 100], np.float32)


class Cover:
    """WorldCover resampled (mode) onto a size x size grid over the map, readable by row window."""

    def __init__(self, geo, wc_dir, size):
        half_g = geo.ground_size_m / 2
        px = geo.ground_size_m / size
        self.size = size
        self.transform = from_origin(-half_g, half_g, px, px)
        self.crs = geo.crs()
        self.tifs = sorted(Path(wc_dir).glob("*.tif"))

    def rows(self, r0, r1):
        r0c, r1c = max(r0, 0), min(r1, self.size)
        out = np.zeros((r1c - r0c, self.size), np.uint8)
        for tif in self.tifs:
            with rasterio.open(tif) as src, WarpedVRT(src, crs=self.crs, transform=self.transform, width=self.size,
                                                       height=self.size, resampling=Resampling.mode, nodata=0) as vrt:
                part = vrt.read(1, window=Window(0, r0c, self.size, r1c - r0c))
            out = np.where(out == 0, part, out)
        pad_top, pad_bot = r0c - r0, r1 - r1c
        if pad_top or pad_bot:
            out = np.pad(out, ((pad_top, pad_bot), (0, 0)), mode="edge")
        return out


def heights_rows(h, size, r0, r1):
    """Heights bilinearly sampled on rows r0..r1 of a size x size grid spanning the same square."""
    n = h.shape[0]
    rows = np.clip(np.arange(r0, r1), 0, size - 1)
    rr = (rows + 0.5) * (n - 1) / size
    cc = (np.arange(size) + 0.5) * (n - 1) / size
    R, C = np.meshgrid(rr, cc, indexing="ij")
    return ndimage.map_coordinates(h, [R, C], order=1, mode="nearest").astype(np.float32)


def fix_cover(lc, land):
    lc = np.where(land & (lc == 0), 30, lc)
    return np.where(land & (lc == 80), 90, lc).astype(np.uint8)


def slope_deg(hs, px):
    dy, dx = np.gradient(hs, px)
    return np.degrees(np.arctan(np.hypot(dx, dy)))


def save_u8(a01, path):
    Image.fromarray((np.clip(a01, 0, 1) * 255 + 0.5).astype(np.uint8)).save(path)


def splats(geo, h, cover4, b):
    S = 4096
    px = geo.size_m / S
    hs = heights_rows(h, S, 0, S)
    land = hs > 0
    lc = fix_cover(cover4.rows(0, S), land)
    sl = slope_deg(hs, px)
    steep = np.clip((sl - 32) / 12, 0, 1).astype(np.float32)
    del sl
    high = np.clip((hs - 3300) / 400, 0, 1).astype(np.float32)
    built = (lc == 50).astype(np.float32)
    rock = np.clip(np.isin(lc, (60, 70, 100)) + steep + high + 0.5 * built, 0, 1).astype(np.float32)
    free = 1 - rock
    layers = {
        "rock": rock,
        "lush": np.isin(lc, (10, 95)) * free,
        "fields": (lc == 40) * free,
        "grass": (np.isin(lc, (20, 30, 90)) + 0.5 * built) * free,
    }
    total = sum(layers.values()) + 1e-6
    for k, w in layers.items():
        save_u8(ndimage.gaussian_filter(np.where(land, w / total, 0).astype(np.float32), 0.8), b / f"splat_{k}.png")
    gm = np.where(land, (np.isin(lc, (20, 30, 90)) + (lc == 40)) * free * 0.25, 0).astype(np.float32)
    save_u8(ndimage.gaussian_filter(gm, 0.6), b / "grass_mask.png")
    shares = {n: float((lc[land] == c).mean()) for n, c in (("tree", 10), ("crop", 40), ("built", 50), ("grass", 30))}
    print("land cover on land:", {k: f"{v*100:.0f}%" for k, v in shares.items()})


def macro_and_chart(geo, h, cover8, b):
    S = 8192
    px = geo.size_m / S
    macro = np.zeros((S, S, 3), np.uint8)
    chart = np.zeros((S, S), np.uint8)
    desert = geo.cfg.get("bareGround", "rock") == "desert"
    for r0 in range(0, S, STRIP):
        a0, a1 = r0 - MARGIN, r0 + STRIP + MARGIN
        hs = heights_rows(h, S, a0, a1)
        land = hs > 0
        lc = fix_cover(cover8.rows(a0, a1), land)
        sl = slope_deg(hs, px)
        steep = np.clip((sl - 32) / 12, 0, 1)
        high = np.clip((hs - 3300) / 400, 0, 1)

        t = np.clip(hs / 3000.0, 0, 1)[..., None]
        col = np.where(t < 0.3, LOW + (HILL - LOW) * (t / 0.3),
                       np.where(t < 0.75, HILL + (MID - HILL) * ((t - 0.3) / 0.45), MID + (PEAK - MID) * ((t - 0.75) / 0.25)))
        layers = [(np.isin(lc, (10, 95)), FOREST, 0.55), (lc == 40, FIELD, 0.5), (lc == 50, TOWN, 0.7)]
        # WorldCover 60 (bare / sparse) and 20 (shrub) used to fall through to the green lowland colour, which
        # turned the Hormuz desert into grassland. Desert maps set "bareGround": "desert" in map.json.
        if desert:
            layers += [(lc == 60, SAND, 0.85), (lc == 20, SCRUB, 0.6)]
        else:
            layers += [(lc == 60, BARE, 0.6)]
        layers += [(lc == 70, PEAK, 0.8), (steep * (1 - high), ROCKC, 0.45)]
        for mask, colour, amount in layers:
            k = ndimage.gaussian_filter(mask.astype(np.float32), 1.5)[..., None] * amount
            col = col * (1 - k) + colour * k
        depth = np.clip(-hs / 25.0, 0, 1)[..., None]
        col = np.where(land[..., None], col, SHALLOW * (1 - depth) + DEEP * depth)
        macro[r0:r0 + STRIP] = np.clip(col[MARGIN:MARGIN + STRIP], 0, 255).astype(np.uint8)

        dy, dx = np.gradient(hs, px)
        s = np.arctan(np.hypot(dx, dy))
        asp = np.arctan2(-dx, dy)
        shade = np.clip(np.sin(np.radians(45)) * np.cos(s) + np.cos(np.radians(45)) * np.sin(s) * np.cos(np.radians(315) - asp), 0, 1)
        c = np.where(land, 26 + 34 * shade, 16.0)
        coast = land ^ ndimage.binary_erosion(land, iterations=2)
        c = np.where(coast, 150, c)
        town = ndimage.gaussian_filter((lc == 50).astype(np.float32), 3.0)
        c = c + 70 * np.clip((town - 0.35) * 2.0, 0, 1)
        chart[r0:r0 + STRIP] = np.clip(c[MARGIN:MARGIN + STRIP], 0, 255).astype(np.uint8)
    Image.fromarray(macro).save(b / "macro.png")
    Image.fromarray(macro).resize((1024, 1024), Image.LANCZOS).save(b / "color.png")
    del macro
    Image.fromarray(chart).convert("RGB").save(b / "map.png")


def ocean(h, b):
    d = heights_rows(h, 2048, 0, 2048)
    save_u8(np.where(d > 0, 1.0, np.clip(1 - (-d) / 70.0, 0, 1) * 0.6), b / "ocean_depth.png")
    dd = np.clip(-d / 70.0, 0, 1)[..., None]
    sand, deep = np.array([171, 162, 136], np.float32), np.array([120, 118, 104], np.float32)
    Image.fromarray((sand * (1 - dd) + deep * dd).astype(np.uint8)).save(b / "ocean_basecolor.png")


def main(cfg_path, wc_dir, build_dir):
    geo = MapGeo.load(cfg_path)
    b = Path(build_dir)
    h = np.load(b / "heights.npy", mmap_mode="r")
    h = np.asarray(h, dtype=np.float32)
    splats(geo, h, Cover(geo, wc_dir, 4096), b)
    ocean(h, b)
    macro_and_chart(geo, h, Cover(geo, wc_dir, 8192), b)
    print("textures written")


if __name__ == "__main__":
    main(*sys.argv[1:])
