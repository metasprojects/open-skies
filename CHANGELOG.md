# Changelog

## 0.1.0 (2026-11-07)

- Island and coast maps keep their buildings. When part of the map was open sea, every building was lost
  (Oahu had none; it now has about 198,000).
- The map engine (`pipeline\` and the Unity builder) is now developed in Open Skies itself.
- `docs\TESTING.md`: what to check on a new map (ground, roads, airbases) and how to report a problem on GitHub.
  It replaces the closed-beta testers' guide.

## 0.1.0-beta.4

- More missions with every map: **Escalation** (PvP, or co-op against the AI: every airbase can be captured, each
  side has factories, SAMs and garrisons; take all the enemy's airbases and destroy their factories) and a
  **Strike** for each side (hit the radar, fuel and ammunition at the enemy's main airbase within 40 minutes). They
  are made from the map's own airbases. One that can't fit on a map is left out with a note; the build never fails
  because of them. The build's last step is now "Missions", and the map's README lists every mission.
- Map engine update: units are no longer placed on mission buildings, other vehicles or town blocks.
- Report a problem: one button saves a zip in the `logs` folder with the logs (newest first, about 20 MB at most),
  the setup check, the last build's status, the Open Skies and Windows versions, and free memory and disk. The
  Windows user name and home folder are taken out; no maps, downloads or setting values go in. Explorer opens with
  the zip selected.
- Update notice: at most once a day, in the background, Open Skies asks GitHub for its releases (test versions
  count) and shows a small notice that you can hide when a newer one is out. It sends nothing about you, and
  offline it stays quiet.
- `docs\TESTERS.md`: a short guide for closed-beta testers (what to try, how to report, known limits).

## 0.1.0-beta.3

- Runway markings: the real runway numbers (from OurAirports), threshold bars, touchdown-zone bars, aiming points,
  centre-line dashes, edge lines and yellow taxi lines, in the game's own marking paint.
- Rivers and lakes on land are painted as water instead of grass.
- Roads: they were drawing all along (checked in the game); from altitude they are thin, like the real ones.

## 0.1.0-beta.2

From the first flight in the game: maps had no buildings and no taxiways, and roads and runways did not show.
- Towns: the real buildings become the game's own houses and shops (up to 60,000 per map), big ones in their real
  place and size, the rest filling the built-up areas along the local streets.
- Forests from the land cover, at about the density of the game's own maps (up to 2.6 million trees).
- Airfields: a raised runway with its proper surface, a parallel taxiway, links and an apron on the flattest side,
  tyre marks, and a floor the aircraft roll on. The Free Flight hangars stand on that apron.

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
