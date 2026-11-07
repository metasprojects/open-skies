# Changelog

## 0.1.0-beta.1 (closed beta)

First version for testers.

- Unzip and double-click `OpenSkies.bat`: first-run setup makes a private Python, finds Nuclear Option, installs
  BepInEx and NOCustomMaps if they are missing, and installs Unity 2022.3.62f2 through Unity Hub once you have signed
  in to it.
- Choose an area on a world map (search, click, drag, resize; up to 261 km square), tick real airports and give
  each a side, name the map, press Build. Every step shows its progress; Stop and Build again carry on where they
  left off.
- Install puts the map and a Free Flight mission into the game: both sides, every aircraft, hangars at every
  airbase, a carrier group per side where there is open sea.
- Export OSM data writes the OpenStreetMap-derived data a map was made from (ODbL).
- Data: Copernicus GLO-30 elevation, ESA WorldCover, Overture Maps (OpenStreetMap), World Bank ship traffic,
  OurAirports, AWS Terrain Tiles. Credits travel inside every map.
