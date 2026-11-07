"""Fetch Overture ferry routes (transportation segments with subtype 'water') for a map, with a small memory budget."""
import sys
from pathlib import Path

from mapgeo import MapGeo
from overture import BASE, connect


def main(cfg_path, out_dir):
    geo = MapGeo.load(cfg_path)
    w, s, e, n = geo.geo_bounds(margin_m=3000)
    out = Path(out_dir)
    for p in out.glob("ferries*.part"):
        p.unlink()
    c = connect(memory="400MB")
    dest = out / "ferries.parquet"
    tmp = out / "ferries.tmp"
    c.execute(f"COPY (SELECT id, names.primary AS name, class, subclass, geometry, bbox "
              f"FROM read_parquet('{BASE}/theme=transportation/type=segment/*', hive_partitioning=0) "
              f"WHERE bbox.xmin < {e} AND bbox.xmax > {w} AND bbox.ymin < {n} AND bbox.ymax > {s} AND subtype = 'water') "
              f"TO '{tmp.as_posix()}' (FORMAT PARQUET)")
    tmp.replace(dest)
    rows = c.execute(f"SELECT count(*), list(DISTINCT name) FROM read_parquet('{dest.as_posix()}')").fetchone()
    print(f"ferries: {rows[0]} segments; named: {[x for x in rows[1] if x][:40]}")


if __name__ == "__main__":
    main(*sys.argv[1:])
