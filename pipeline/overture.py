"""Overture Maps access through DuckDB (anonymous S3, GeoParquet, bbox pushdown).

Overture (https://overturemaps.org) is one global, monthly dataset built largely from
OpenStreetMap plus ML building footprints: roads (transportation/segment), buildings with
heights (buildings/building), airports and other infrastructure (base/infrastructure), water
and land use (base/water, base/land_use). Licences: ODbL for OSM-derived themes, CDLA-Permissive-2.0
for the rest; credit "(c) OpenStreetMap contributors, Overture Maps Foundation".

DuckDB keeps its extensions and spill files on G: (C: is full on the Deck).

Usage:
    overture.py describe
    overture.py fetch <map.json> <out_dir>
"""
import os
import sys
import time
from pathlib import Path

import duckdb

CACHE = os.environ.get("NOEARTH_DUCKDB_DIR", str(Path(__file__).resolve().parents[1] / "data" / "cache" / "duckdb"))
PINNED = "2026-10-21.0"


def resolve_release():
    """The release to read: NOEARTH_OVERTURE_RELEASE if set, else the pinned one while it is still online, else
    the newest one listed. Overture keeps only about three releases on S3, so a pin goes stale in months."""
    if os.environ.get("NOEARTH_OVERTURE_RELEASE"):
        return os.environ["NOEARTH_OVERTURE_RELEASE"]
    try:
        import re
        import urllib.request
        url = "https://overturemaps-us-west-2.s3.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"
        with urllib.request.urlopen(url, timeout=30) as r:
            listed = sorted(set(re.findall(r"<Prefix>release/([^/<]+)/</Prefix>", r.read().decode())))
    except Exception as e:                                   # offline or blocked: keep the pin
        print(f"  could not list Overture releases ({e}); using {PINNED}")
        return PINNED
    if PINNED in listed or not listed:
        return PINNED
    print(f"  Overture release {PINNED} is no longer online; using the newest, {listed[-1]}")
    return listed[-1]


RELEASE = resolve_release()
BASE = f"s3://overturemaps-us-west-2/release/{RELEASE}"

TYPES = {
    "roads": ("transportation", "segment"),
    "buildings": ("buildings", "building"),
    "infrastructure": ("base", "infrastructure"),
    "water": ("base", "water"),
    "land_use": ("base", "land_use"),
    "places": ("places", "place"),
    "ferries": ("transportation", "segment"),
}


def connect(memory=None):
    memory = memory or os.environ.get("NOEARTH_DUCKDB_MEM", "900MB")
    for d in ("ext", "tmp"):
        os.makedirs(os.path.join(CACHE, d), exist_ok=True)
    c = duckdb.connect(config={"extension_directory": os.path.join(CACHE, "ext"),
                               "temp_directory": os.path.join(CACHE, "tmp"),
                               "memory_limit": memory, "threads": 2})
    for ext in ("httpfs", "spatial"):
        c.execute(f"INSTALL {ext}; LOAD {ext};")
    c.execute("SET s3_region='us-west-2';")
    return c


def src(kind):
    theme, typ = TYPES[kind]
    return f"read_parquet('{BASE}/theme={theme}/type={typ}/*', hive_partitioning=0)"


def describe(c, only=None):
    for kind in ([only] if only else TYPES):
        cols = c.execute(f"DESCRIBE SELECT * FROM {src(kind)} LIMIT 0").fetchall()
        print(f"{kind}: " + ", ".join(f"{n} {t[:40]}" for n, t, *_ in cols))


def fetch(c, cfg_path, out_dir):
    sys.path.insert(0, str(Path(__file__).parent))
    from mapgeo import MapGeo
    geo = MapGeo.load(cfg_path)
    w, s, e, n = geo.geo_bounds(margin_m=3000)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    where = f"bbox.xmin < {e} AND bbox.xmax > {w} AND bbox.ymin < {n} AND bbox.ymax > {s}"
    filters = {
        "roads": "AND subtype = 'road' AND class IN ('motorway','trunk','primary','secondary','tertiary','unclassified','residential')",
        "buildings": "",
        "infrastructure": "AND subtype IN ('airport','bridge','pier','power','tower','transit','barrier')",
        "water": "",
        "land_use": "",
        "places": "AND confidence >= 0.5",
        "ferries": "AND subtype = 'water'",
    }
    columns = {
        "buildings": "id, names.primary AS name, height, num_floors, min_height, is_underground, subtype, class, "
                     "has_parts, geometry, bbox",
        "places": "id, names.primary AS name, basic_category, taxonomy.primary AS category, "
                  "taxonomy.hierarchy AS hierarchy, confidence, operating_status, geometry, bbox",
    }
    c.execute("SET preserve_insertion_order = false;")
    for kind in TYPES:
        dest = out / f"{kind}.parquet"
        if dest.exists():
            print(f"  {kind}: cached")
            continue
        t0 = time.time()
        cols = columns.get(kind, "*")
        parts = 4 if kind == "buildings" else 1
        pieces = []
        for k in range(parts):
            xa = w + (e - w) * k / parts
            xb = w + (e - w) * (k + 1) / parts
            # A feature belongs to the strip that holds its bbox's west edge, so none is fetched twice.
            strip = f"bbox.xmin >= {xa} AND bbox.xmin < {xb}" if parts > 1 else "TRUE"
            if k == 0 and parts > 1:
                strip = f"bbox.xmin < {xb}"
            # A piece is written under .writing and renamed only once complete, so a crash or out-of-memory
            # half-way never leaves a file the next run would take as finished (a 0-byte buildings.0.part did).
            piece = out / f"{kind}.{k}.part"
            if not piece.exists():
                writing = out / f"{kind}.{k}.writing"
                writing.unlink(missing_ok=True)
                c.execute(f"COPY (SELECT {cols} FROM {src(kind)} WHERE {where} AND {strip} {filters[kind]}) "
                          f"TO '{writing.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
                writing.replace(piece)
            pieces.append(piece)
        if len(pieces) == 1:
            pieces[0].replace(dest)                          # one piece: it already is the file
        else:
            listing = ", ".join(f"'{p.as_posix()}'" for p in pieces)
            tmp = dest.with_suffix(".writing")
            c.execute(f"COPY (SELECT * FROM read_parquet([{listing}])) TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            tmp.replace(dest)
            for p in pieces:
                p.unlink()
        rows = c.execute(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}')").fetchone()[0]
        print(f"  {kind}: {rows:,} features, {dest.stat().st_size/1e6:.1f} MB in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    con = connect()
    if sys.argv[1] == "describe":
        describe(con, sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        fetch(con, sys.argv[2], sys.argv[3])
