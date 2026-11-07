# Open Skies

Make a map of any real place for Nuclear Option: real terrain, coastlines, sea depth, land cover, roads, towns,
ship lanes and real airbases, built from open data. Then fly it.

## How to use it

1. **Unzip** the Open Skies folder anywhere with about 15 GB free (not inside Program Files). Windows marks files
   from the internet: right-click the zip, choose Properties, tick **Unblock**, then unzip, or Windows asks before
   every script runs.
2. **Double-click `OpenSkies.bat`.** The first time, setup runs by itself: it makes a private copy of Python, finds
   Nuclear Option, and installs the mod loader (BepInEx) and the map loader (NOCustomMaps) if you don't have them.
   It is safe to run again (`Setup.bat`) at any time.
3. **Sign in to Unity Hub** when Open Skies asks. Unity is what packs the map into the game's format; it needs a
   free Unity account and this is the one step you have to do by hand. Open Skies then installs the right Unity
   version (2022.3.62f2) for you.
4. **Choose an area.** Search for a place, or click the map. The box is the map: 261 km square at full size.
   It turns red if it can't be built.
5. **Choose airbases.** Every real airport in the box is listed. Tick the ones you want and give each a side
   (Boscali or Primeva). Short runways are marked: those are for helicopters and light aircraft.
6. **Name it and press Build.** A full-size map takes about 30 to 60 minutes. Downloads are kept, so the next map
   in the same part of the world is quicker. If a step fails, the message says why and what to do; pressing
   Build again carries on from that step.
7. **Press Install.** The map and a Free Flight mission (both sides, every aircraft, no enemies) go into the
   game. Start Nuclear Option and fly.

Anything missing shows up in the **Setup incomplete** panel with the exact fix.

## What you need

- Windows 10 or 11, 64-bit
- Nuclear Option (Steam)
- 8 GB of memory or more (16 GB is comfortable): Unity needs it to make the map file
- About 15 GB of free disk space while a map builds (a finished map is 1 to 3 GB)
- An internet connection (the open data is downloaded as needed)
- A free Unity account

## Multiplayer

Every player needs the same build of the map. Share the `.nomap` file from the map's folder (Open folder), not a
rebuild: building again changes the map's fingerprint, and the game then treats it as a different map.

## Data and credits

Every map carries its credits inside it and in `README.txt` next to it:
Copernicus WorldDEM-30 (DLR, Airbus, EU/ESA), ESA WorldCover 2021 (CC BY 4.0), OpenStreetMap contributors and
Overture Maps Foundation (ODbL), World Bank/IMF ship traffic (CC BY 4.0), OurAirports (public domain), AWS
Terrain Tiles (sea floor). The OpenStreetMap-derived data of any map can be exported with **Export OSM data**,
as the ODbL asks. See `LICENSES.md`.

This project is an unofficial community modification and is not affiliated with, sponsored by, or endorsed by Shockfront Studios Pty Ltd. Original Nuclear Option assets, vehicle designs, audio, and code are Copyright (c) 2026 Shockfront Studios Pty Ltd. All rights reserved. Nuclear Option and Shockfront Studios are trademarks or registered trademarks of Shockfront Studios Pty Ltd. Original mod content and all other trademarks belong to their respective owners.
Open Skies contains no game files. Mods for Nuclear Option may not be sold or put behind a paywall.

## Problems

- Logs are in the `logs` folder (the **Open logs folder** button opens it).
- Running `Setup.bat` again fixes most setup problems.
- A build that stopped carries on from where it stopped: just press Build again.
