# Licences and credits

## Open Skies itself
MIT, copyright (c) 2026 Meta. See `LICENSE.txt`.

## Work Open Skies builds on

- **KnoxMap** by euclid80tr (MIT): Open Skies follows its shape (unzip, double-click, first-run setup, a local
  map window). `Setup.bat`'s private-Python download and the window launcher in `openskies.py` are adapted from
  KnoxMap's MIT-licensed `Setup.bat` and `knoxmap.py`:

  > MIT License. Copyright (c) 2026 spytheeuclidean-a11y.
  > Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
  > documentation files (the "Software"), to deal in the Software without restriction, including without
  > limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
  > Software, and to permit persons to whom the Software is furnished to do so, subject to the following
  > conditions: The above copyright notice and this permission notice shall be included in all copies or
  > substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
  > OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE
  > AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
  > OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
  > WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

- **NOCustomMaps** by BatongMaster (MIT): the loader the maps are made for. Setup downloads its official release
  (1.4.2) from GitHub and checks its fingerprint; Open Skies follows its map format and naming rules.
- **BepInEx** (LGPL-2.1): the mod loader. Setup downloads the official release (5.4.23.4) and checks it.
- **Leaflet** 1.9.4 (BSD-2-Clause, (c) Volodymyr Agafonkin): bundled in `static/vendor/leaflet` with its licence.
- **Unity** 2022.3.62f2: installed through Unity Hub under the player's own free Unity licence. Not bundled.
- **Python** 3.13.7 (PSF License): setup downloads the official python.org build (published as a NuGet package).
- Python packages (installed by setup, not bundled): Flask, pywebview, requests, NumPy, SciPy, rasterio/GDAL,
  pyproj/PROJ, Shapely/GEOS, DuckDB, scikit-image, Pillow, each under its own licence.

## Data (downloaded when a map is built, never bundled)

| Data | Source | Licence | Credit carried in every map |
|---|---|---|---|
| Elevation | Copernicus DEM GLO-30, AWS Open Data | Copernicus DEM licence | the full modified-data notice and liability sentence |
| Land cover | ESA WorldCover 10 m 2021 v200 | CC BY 4.0 | yes |
| Roads and ferries | Overture Maps transportation: (c) OpenStreetMap contributors, TomTom | ODbL 1.0 | yes; the OpenStreetMap data is exported on request (Export OSM data) |
| Buildings | Overture Maps buildings: (c) OpenStreetMap contributors; Esri Community Maps contributors (CC BY 4.0); Microsoft Global ML Building Footprints (ODbL); Google Open Buildings (CC BY 4.0); USGS 3D Elevation Program; Qian Shi et al. 2023, doi:10.5281/zenodo.8174931 (CC BY 4.0); work derived from BTN 2024, ign.es (CC BY 4.0) | ODbL 1.0 | short form in the map, full list in its README.txt |
| Water, land use, infrastructure | Overture Maps base: (c) OpenStreetMap contributors; Daylight Map Distribution; ESA WorldCover 2020 (CC BY 4.0); ETOPO1 (ODC PDDL); GLOBathy (CC0) | ODbL 1.0 | short form in the map, full list in its README.txt |
| Places | Overture Maps places: data from Meta, DAC, BrightQuery, Uberall and OpenTravel Alliance | CDLA Permissive 2.0 | short form in the map, full list in its README.txt |
| Ship traffic | World Bank / IMF Global Shipping Traffic Density | CC BY 4.0 | yes |
| Airports and runways | OurAirports | public domain | yes |
| Sea floor | AWS Terrain Tiles (ETOPO1, GEBCO, NOAA) | per the tilezen/joerd attribution list (https://github.com/tilezen/joerd/blob/master/docs/attribution.md) | in README.txt, with that link |
| Map background, search | OpenStreetMap tiles, Nominatim | ODbL, usage policies | on screen |

Map tiles and searches go through Open Skies' own local server with its own User-Agent and a cache, as the
OpenStreetMap tile and Nominatim usage policies ask.

## Not affiliated
This project is an unofficial community modification and is not affiliated with, sponsored by, or endorsed by Shockfront Studios Pty Ltd. Original Nuclear Option assets, vehicle designs, audio, and code are Copyright (c) 2026 Shockfront Studios Pty Ltd. All rights reserved. Nuclear Option and Shockfront Studios are trademarks or registered trademarks of Shockfront Studios Pty Ltd. Original mod content and all other trademarks belong to their respective owners.

Open Skies contains no game files or game code. Mods for Nuclear Option may not be sold or put behind a paywall.
