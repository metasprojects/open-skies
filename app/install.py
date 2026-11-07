"""Putting things into the game: BepInEx and NOCustomMaps (once), then each finished map and its mission.

Downloads are pinned and checked by SHA-256 before anything is unpacked. Nothing is installed while the game is
running, and only files Open Skies made are ever replaced.
"""
from __future__ import annotations

import shutil
import threading
import time
import zipfile
from pathlib import Path

from . import downloads, paths


class InstallError(Exception):
    def __init__(self, title, fix=""):
        super().__init__(title)
        self.title, self.fix = title, fix


def game_running() -> bool:
    return bool(paths.process_running(paths.GAME_EXE))


def _need_game() -> Path:
    game = paths.game_dir()
    if not game:
        raise InstallError("Nuclear Option was not found.",
                           "Install it from Steam, or choose its folder in Setup (Change game folder).")
    return game


def _safe_extract(z: zipfile.ZipFile, dest: Path, prefix: str = "") -> int:
    n = 0
    root = dest.resolve()
    for info in z.infolist():
        if info.is_dir():
            continue
        target = (dest / prefix / info.filename).resolve()
        if root not in target.parents:
            raise InstallError(f"The download contains a bad path ({info.filename}); nothing more was unpacked.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        n += 1
    return n


def install_mods() -> list[str]:
    """BepInEx 5 and NOCustomMaps, if either is missing. Returns what was done, in plain words."""
    game = _need_game()
    if game_running():
        raise InstallError("Nuclear Option is running.", "Close the game, then press Install again.")
    done = []
    dl = paths.CACHE / "installers"
    if not paths.bepinex_ok(game):
        z = downloads.fetch(downloads.BEPINEX["url"], dl / Path(downloads.BEPINEX["url"]).name,
                            sha256=downloads.BEPINEX["sha256"])
        with zipfile.ZipFile(z) as zf:
            _safe_extract(zf, game)
        done.append(f"Installed BepInEx {downloads.BEPINEX['version']} into {game}")
    if not paths.nocustommaps_dll(game):
        z = downloads.fetch(downloads.NOCUSTOMMAPS["url"], dl / Path(downloads.NOCUSTOMMAPS["url"]).name,
                            sha256=downloads.NOCUSTOMMAPS["sha256"])
        target = game / "BepInEx" / "plugins" / "NOCustomMaps"
        with zipfile.ZipFile(z) as zf:
            _safe_extract(zf, target)
        done.append(f"Installed NOCustomMaps {downloads.NOCUSTOMMAPS['version']} (MIT, by BatongMaster)")
    paths.maps_dir(game).mkdir(parents=True, exist_ok=True)
    return done or ["BepInEx and NOCustomMaps were already installed."]


# ---- maps -----------------------------------------------------------------------------------------

_COPY = {"running": False, "mapId": None, "percent": 0, "error": None, "done": None}


def copy_status() -> dict:
    return dict(_COPY)


def install_map(mid: str) -> dict:
    """Start copying a finished map and its missions into the game (the .nomap is 1-3 GB: done in the background)."""
    from .build import built_map
    info = built_map(mid)
    if not info:
        raise InstallError("That map has not finished building yet.")
    game = _need_game()
    if not paths.bepinex_ok(game) or not paths.nocustommaps_dll(game):
        raise InstallError("BepInEx and NOCustomMaps are not installed in the game yet.",
                           "Open Setup and press Install mods, then Install again.")
    if game_running():
        raise InstallError("Nuclear Option is running.",
                           "Close the game first: it keeps the map files open. Then press Install again.")
    if _COPY["running"]:
        raise InstallError("Already installing a map.")
    src = paths.OUT / mid / f"{mid}.nomap"
    dest_dir = paths.maps_dir(game)
    need = src.stat().st_size / 2**30 + 0.2
    if paths.free_gb(dest_dir) < need:
        raise InstallError(f"Not enough space on the game's drive (needs {need:.1f} GB).", "Free some space there.")
    _COPY.update(running=True, mapId=mid, percent=0, error=None, done=None)

    def work():
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest_dir / f"{mid}.nomap.openskies-part"
            total = src.stat().st_size
            copied = 0
            with open(src, "rb") as a, open(tmp, "wb") as b:
                while True:
                    buf = a.read(8 << 20)
                    if not buf:
                        break
                    b.write(buf)
                    copied += len(buf)
                    _COPY["percent"] = round(100 * copied / total)
            # An older .nomapz of the same map would register under the same id: the new build replaces it.
            for old in (dest_dir / f"{mid}.nomapz",):
                old.unlink(missing_ok=True)
            final = dest_dir / f"{mid}.nomap"
            final.unlink(missing_ok=True)
            tmp.replace(final)
            mdir = paths.missions_dir()
            mdir.mkdir(parents=True, exist_ok=True)
            names = []
            for m in sorted((paths.OUT / mid / "missions").iterdir()):
                if m.is_dir():
                    target = mdir / m.name
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(m, target)
                    names.append(m.name)
            _COPY.update(done={"map": str(final), "missions": names, "missionsDir": str(mdir),
                               "at": time.strftime("%H:%M")})
            (paths.OUT / mid / "installed.txt").write_text(f"{final}\n" + "\n".join(str(mdir / n) for n in names),
                                                           encoding="utf-8")
        except OSError as e:
            _COPY["error"] = {"title": f"Could not copy the map into the game: {e.strerror or e}",
                              "fix": "Close the game and anything else using its folder, then press Install again."}
        finally:
            _COPY["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return copy_status()


def export_osm(mid: str) -> Path:
    """The OpenStreetMap-derived data used for a map, as the ODbL asks: one zip with the Overture extracts."""
    import json
    cfg = json.loads((paths.MAPS / mid / "map.json").read_text(encoding="utf-8"))
    ovs = sorted((paths.CACHE / "overture").glob(f"{mid}-*"), key=lambda p: p.stat().st_mtime)
    if not ovs:
        raise InstallError("This map has no OpenStreetMap data (none was downloaded for it).")
    ov = ovs[-1]
    dest = paths.OUT / mid / f"{mid}-osm-data.zip"
    readme = (f"OpenStreetMap-derived data used for the Nuclear Option map '{cfg['displayName']}' ({mid}), "
              "made with Open Skies.\n\n"
              "Roads, buildings, ferry routes, places, water, land use and infrastructure inside the map area, as "
              "GeoParquet files extracted from Overture Maps (https://overturemaps.org).\n"
              "(c) OpenStreetMap contributors, Overture Maps Foundation. Available under the Open Database License "
              "(ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/ . Non-OSM parts of Overture are under "
              "CDLA-Permissive-2.0.\n")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        z.writestr("README.txt", readme)
        for f in sorted(ov.glob("*.parquet")):
            z.write(f, f.name)
    return dest
