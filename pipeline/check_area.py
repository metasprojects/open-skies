"""Print where each must-include place lands on the map and whether it clears the edge fade."""
import sys

from mapgeo import MapGeo


def main(cfg_path, tiles=None, scale=None):
    geo = MapGeo.load(cfg_path)
    if tiles:
        geo.cfg["tilesPerSide"] = int(tiles)
    if scale:
        geo.cfg["horizontalScale"] = float(scale)
    half = geo.size_m / 2
    fade = geo.cfg.get("edgeFadeMetres", 0)
    print(f"map {geo.size_m/1000:.2f} km game, {geo.ground_size_m/1000:.2f} km ground, {geo.samples} samples/side")
    w, s, e, n = geo.geo_bounds(0)
    print(f"bounds lon {w:.3f}..{e:.3f}  lat {s:.3f}..{n:.3f}")
    ok = True
    for name, (lat, lon) in geo.cfg["mustInclude"].items():
        x, z = geo.game_xz(lat, lon)
        margin = half - max(abs(x), abs(z))
        flag = "ok" if margin > fade else ("EDGE" if margin > 0 else "OUT")
        ok &= flag == "ok"
        print(f"  {name:22s} x={x/1000:7.1f} km z={z/1000:7.1f} km  {margin/1000:6.1f} km from edge  {flag}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
