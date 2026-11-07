"""Shared map geometry: the map's local metric frame and its geographic bounds.

The map frame is a transverse Mercator projection centred on the map centre, so
x is metres east and y is metres north of the centre, with scale error under
0.1 % across a 260 km map. Game coordinates use x east, z north, y up.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from pyproj import CRS, Transformer


@dataclass
class MapGeo:
    cfg: dict

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def lat0(self):
        return self.cfg["center"]["lat"]

    @property
    def lon0(self):
        return self.cfg["center"]["lon"]

    @property
    def size_m(self):
        """Side of the map in game metres."""
        return self.cfg["tileMetres"] * self.cfg["tilesPerSide"]

    @property
    def scale(self):
        """Real metres per game metre."""
        return self.cfg.get("horizontalScale", 1.0)

    @property
    def ground_size_m(self):
        """Side of the covered area on the real ground."""
        return self.size_m * self.scale

    @property
    def samples(self):
        """Height samples per side: one per vertex, shared at tile seams."""
        pitch = self.cfg["vertexPitchMetres"]
        return int(round(self.size_m / pitch)) + 1

    def crs(self):
        return CRS.from_proj4(
            f"+proj=tmerc +lat_0={self.lat0} +lon_0={self.lon0} +k=1 +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs"
        )

    def to_local(self):
        if getattr(self, "_to_local", None) is None:
            self._to_local = Transformer.from_crs("EPSG:4326", self.crs(), always_xy=True)
        return self._to_local

    def to_geo(self):
        if getattr(self, "_to_geo", None) is None:
            self._to_geo = Transformer.from_crs(self.crs(), "EPSG:4326", always_xy=True)
        return self._to_geo

    def geo_bounds(self, margin_m=2000):
        """(west, south, east, north) in degrees, covering the ground area plus a margin."""
        half = self.ground_size_m / 2 + margin_m
        t = self.to_geo()
        xs, ys = [], []
        for x in (-half, 0, half):
            for y in (-half, 0, half):
                lon, lat = t.transform(x, y)
                xs.append(lon)
                ys.append(lat)
        return min(xs), min(ys), max(xs), max(ys)

    def game_xz(self, lat, lon):
        """Game coordinates (x east, z north) of a point, in game metres."""
        x, y = self.to_local().transform(lon, lat)
        return x / self.scale, y / self.scale
