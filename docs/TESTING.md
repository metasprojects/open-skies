# Checking a map and reporting problems

Open Skies turns a real place into a Nuclear Option map: the terrain, coastline, sea, towns, roads and real
airbases. This page says what to check on a new map and how to report anything that goes wrong.

You need Windows 10 or 11 (64-bit), Nuclear Option on Steam, 8 GB of memory or more, about 15 GB of free disk
space, an internet connection and a free Unity account. `README.md` has the full steps.

## First run

Before you unzip, right-click the zip, choose Properties and tick **Unblock**. Then double-click `OpenSkies.bat`
and follow what it says. Setup works on a PC that has never had BepInEx or NOCustomMaps, and on one that uses the
Nuclear Option Mod Manager (NOMM). Signing in to Unity Hub is the one step you do by hand.

## What to check on a new map

1. **Build it.** Pick a place. A 108 km map is a quick first test; full size (261 km) takes 30 to 60 minutes.
2. **The ground.** Coastline, hills and mountains, rivers and lakes, the sea, towns.
3. **Roads.** Main roads link the towns, follow the ground, and cross rivers on bridges.
4. **Airbases.** Runways sit where the real ones are, flat and paved, with their numbers painted on. Taxiways,
   aprons and the roads into the airbase are there.
5. **Fly it.** Press **Install**, start Nuclear Option and play the map's Free Flight mission on both sides. Take
   off and land at a land airbase, then start from the carrier if the map has one. Note how smoothly it runs.
6. **Fight on it.** Most maps also get an Escalation mission (PvP, or co-op against the AI: capture the enemy's
   airbases and destroy their factories) and a Strike for each side (hit the radar, fuel and ammunition at the
   enemy's main airbase within 40 minutes).
7. **Multiplayer.** Every player needs the same build of the map. Press **Open folder** on the map, send the
   `.nomap` file to a friend, have them put it in the same place, then host and join. Don't build the map again
   on the other PC: a rebuild counts as a different map.

## What to report

- Anything that fails or stops, with the message Open Skies showed, word for word.
- Anything that looks wrong in the game: missing or floating things, odd terrain, roads, airbases, water.
- Steps that took much longer than Open Skies said they would.
- Words or buttons that didn't make sense.

## How to report it

Press **Report a problem** at the top right of Open Skies (it is also next to **Open logs folder** when a build
stops). Open Skies saves one zip file in its `logs` folder and opens a window with that file selected. Open an
issue at https://github.com/metasprojects/open-skies/issues, attach that zip, and say in a few lines what you did,
what happened and what you expected. Screenshots help.

The zip holds the Open Skies logs, the setup check, the Windows version, and free memory and disk space. Your
Windows user name and home folder are taken out. It has no maps, no downloaded data and no settings values. You
can open it and look before you send it.

## Updates

Once a day Open Skies asks GitHub whether a newer version is out and shows a small notice at the top if one is.
It sends nothing about you or your PC. New versions are downloaded by hand: unzip the new one into a new folder.

## Known limits

- Unity needs about 8 GB of memory. On a smaller PC, Open Skies can prepare a map but not finish it.
- A full-size map takes 30 to 60 minutes and about 15 GB of free disk space while it builds. A finished map is
  1 to 3 GB.
- The Escalation and Strike missions are made from the map's airbases. Where they can't fit (for example a side
  whose airbases have no flat room for hangars and no sea for a carrier), the map gets Free Flight only and the
  build says why.
- Maps are installed on your own PC. The map loader can't load maps from the Steam Workshop yet, so players share
  the `.nomap` file directly.
- Roads are thin from high up, like real roads.
- A game update that changes the game's Unity version needs a new Open Skies.
