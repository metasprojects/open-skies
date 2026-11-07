"""Pictures of the road network over a dark hillshade: the whole map and close-ups.

Usage (run from tools/):
    roads_preview.py <map.json> <build_dir>

Reads <build_dir>/roads.bytes, roads_draw.json, heights.npy and airbases.json. Writes
roads_preview.png (2048 px, whole map), roads_preview_taipei.png and roads_preview_taichung.png
(1600 px, 25 km across). Colour by class; bridges cyan, causeways teal, tunnels magenta; airbase
pads outlined red.
"""
import json
import math
import os
import sys
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from mapgeo import MapGeo  # noqa: E402
from roads import read_roads  # noqa: E402

CLASS_COLOUR = [(255, 140, 60), (255, 210, 80), (235, 235, 235), (110, 160, 230)]
CLASS_NAME = ["motorway", "trunk", "primary", "other"]
KIND_COLOUR = {1: (230, 70, 230), 2: (0, 230, 255), 3: (0, 200, 160)}
KIND_NAME = {1: "tunnel (not drawn in game)", 2: "bridge", 3: "causeway (DEM sea)"}
CLOSEUPS = {"taipei": (108000.0, -6500.0), "taichung": (17000.0, -95000.0)}
SPAN = 25000.0


def font(size):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def hillshade(h, pitch):
    dz, dx = np.gradient(h.astype(np.float32), pitch)
    slope = np.arctan(np.hypot(dx, dz))
    aspect = np.arctan2(-dx, dz)
    az, alt = math.radians(315), math.radians(40)
    shade = np.clip(math.sin(alt) * np.cos(slope) + math.cos(alt) * np.sin(slope) * np.cos(az - aspect), 0, 1)
    land = h >= 0
    v = np.where(land, 28 + 70 * shade, 0)
    rgb = np.stack([np.where(land, v, 10), np.where(land, v, 16), np.where(land, v * 1.02, 30)], -1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


def base_image(h, half, pitch, x0, z0, span, size):
    """Hillshade of the square with top-left (x0, z0) and side span, at size px."""
    n = h.shape[0]
    c0 = int(max(0, math.floor((x0 + half) / pitch)))
    c1 = int(min(n - 1, math.ceil((x0 + span + half) / pitch)))
    r0 = int(max(0, math.floor((half - z0) / pitch)))
    r1 = int(min(n - 1, math.ceil((half - (z0 - span)) / pitch)))
    stride = max(1, int((c1 - c0) / size))
    sub = np.asarray(h[r0:r1 + 1:stride, c0:c1 + 1:stride])
    img = hillshade(sub, pitch * stride)
    # Place the sampled block where it falls in the requested square.
    mpp = span / size
    left = ((-half + c0 * pitch) - x0) / mpp
    top = (z0 - (half - r0 * pitch)) / mpp
    w = (c1 - c0) * pitch / mpp
    hh = (r1 - r0) * pitch / mpp
    out = Image.new("RGB", (size, size), (10, 16, 30))
    out.paste(img.resize((max(1, int(round(w))), max(1, int(round(hh)))), Image.BILINEAR), (int(round(left)), int(round(top))))
    return out


def draw_roads(img, recs, draw, x0, z0, span, closeup):
    size = img.width
    mpp = span / size
    d = ImageDraw.Draw(img)
    kinds = draw["kind"]
    widths = draw["width"]

    def px(p):
        return list(zip(((p[:, 0] - x0) / mpp).tolist(), ((z0 - p[:, 2]) / mpp).tolist()))

    def visible(p):
        return not (p[:, 0].max() < x0 or p[:, 0].min() > x0 + span or p[:, 2].min() > z0 or p[:, 2].max() < z0 - span)

    def lw(i, cls):
        if closeup:
            return max(2, int(round(widths[i] / mpp * 1.6)))
        return [3, 2, 2, 1][cls]

    order = sorted(range(len(recs)), key=lambda i: (-recs[i]["cls"], kinds[i]))
    for i in order:
        r = recs[i]
        if kinds[i] or not visible(r["pts"]):
            continue
        d.line(px(r["pts"]), fill=CLASS_COLOUR[r["cls"]], width=lw(i, r["cls"]), joint="curve")
    for k in (3, 2, 1):
        for i in order:
            r = recs[i]
            if kinds[i] != k or not visible(r["pts"]):
                continue
            w = lw(i, r["cls"]) + (1 if closeup else 0)
            if k == 1:
                w = max(1, w - 1)
            d.line(px(r["pts"]), fill=KIND_COLOUR[k], width=w, joint="curve")


def draw_pads(img, pads, x0, z0, span):
    mpp = span / img.width
    d = ImageDraw.Draw(img)
    for ab in pads:
        h = math.radians(ab["heading"])
        ux, uz = math.sin(h), math.cos(h)
        vx, vz = uz, -ux
        pts = []
        for sa, sc in ((1, 1), (1, -1), (-1, -1), (-1, 1), (1, 1)):
            x = ab["x"] + sa * ab["flatHalfAlong"] * ux + sc * ab["flatHalfAcross"] * vx
            z = ab["z"] + sa * ab["flatHalfAlong"] * uz + sc * ab["flatHalfAcross"] * vz
            pts.append(((x - x0) / mpp, (z0 - z) / mpp))
        d.line(pts, fill=(230, 40, 40), width=2)


def legend(img, title, span, counts, top=10):
    d = ImageDraw.Draw(img)
    f, fs = font(max(14, img.width // 85)), font(max(12, img.width // 110))
    lh = int(f.size * 1.35)
    rows = [(CLASS_COLOUR[c], f"{CLASS_NAME[c]}  {counts[c]:,.0f} km") for c in range(4)]
    rows += [(KIND_COLOUR[k], KIND_NAME[k]) for k in (2, 3, 1)] + [((230, 40, 40), "airbase pad")]
    w = int(img.width * 0.27)
    d.rectangle([10, top, 10 + w, top + 10 + lh * (len(rows) + 1)], fill=(0, 0, 0))
    d.text((20, top + 4), title, fill=(255, 255, 255), font=f)
    for j, (col, text) in enumerate(rows):
        y = top + 6 + lh * (j + 1)
        d.line([(20, y + lh // 2), (50, y + lh // 2)], fill=col, width=4)
        d.text((60, y), text, fill=(230, 230, 230), font=f)
    bar = 5000 if span < 60000 else 50000
    bpx = bar / span * img.width
    y = img.height - 40
    d.rectangle([20, y, 20 + bpx, y + 8], fill=(240, 240, 240))
    d.text((20, y - lh), f"{bar // 1000} km", fill=(240, 240, 240), font=f)
    d.text((img.width - 10, img.height - 10), "Roads (c) OpenStreetMap contributors, Overture Maps Foundation; "
           "terrain Copernicus DEM", fill=(170, 170, 170), font=fs, anchor="rd")


def main(cfg_path, build_dir):
    geo = MapGeo.load(cfg_path)
    build = Path(build_dir)
    half = geo.size_m / 2
    pitch = geo.cfg["vertexPitchMetres"]
    h = np.load(build / "heights.npy", mmap_mode="r")
    if not (build / "roads.bytes").exists():
        print("no road network on this map: no roads preview")
        return
    recs = read_roads(build / "roads.bytes")
    draw = json.loads((build / "roads_draw.json").read_text(encoding="utf-8"))
    pads = json.loads((build / "airbases.json").read_text(encoding="utf-8"))
    km = [0.0] * 4
    for r in recs:
        p = r["pts"]
        km[r["cls"]] += float(np.hypot(np.diff(p[:, 0]), np.diff(p[:, 2])).sum()) / 1000
    img = base_image(h, half, pitch, -half, half, 2 * half, 2048)
    draw_roads(img, recs, draw, -half, half, 2 * half, False)
    draw_pads(img, pads, -half, half, 2 * half)
    # Bottom left is open sea on this map.
    legend(img, f"{geo.cfg['displayName']}: road network, {len(recs):,} records", 2 * half, km, top=int(img.height * 0.62))
    img.save(build / "roads_preview.png")
    # Close-ups: the map's own "closeups" (lat, lon) from map.json; Taiwan's two only on the Taiwan map.
    closeups = ({k: geo.game_xz(*v) for k, v in geo.cfg["closeups"].items()} if geo.cfg.get("closeups")
                else CLOSEUPS if geo.cfg["mapId"] == "taiwanstrait" else {})
    for name, (cx, cz) in closeups.items():
        x0, z0 = cx - SPAN / 2, cz + SPAN / 2
        img = base_image(h, half, pitch, x0, z0, SPAN, 1600)
        draw_roads(img, recs, draw, x0, z0, SPAN, True)
        draw_pads(img, pads, x0, z0, SPAN)
        legend(img, f"{name.title()}: 25 km across, centre x={cx:.0f} z={cz:.0f}", SPAN, km)
        img.save(build / f"roads_preview_{name}.png")
    print(f"wrote roads_preview.png and {len(closeups)} close-ups in {build}")


if __name__ == "__main__":
    main(*sys.argv[1:])
