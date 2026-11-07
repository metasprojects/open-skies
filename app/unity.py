"""Unity: install the editor through Unity Hub's command line, and run the batch build that makes the .nomap.

The builder project is Open Skies' own copy (unity/OpenSkiesBuilder: two editor scripts, no game files), set up
in the data folder so Unity's Library cache survives between builds.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import paths

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROJECT = paths.DATA / "unity" / "OpenSkiesBuilder"
MIN_RAM_MB = 7500          # 2601 tiles with three levels of detail: Unity peaks at several GB


# ---- installing the editor through Unity Hub ----------------------------------------------------------

_INSTALL = {"running": False, "lines": [], "ok": None, "started": None}


def hub_line(line: str) -> str:
    """Unity Hub's progress output without its terminal control codes and bare 'Progress:' lines."""
    line = re.sub(r"\[[0-9;]*[A-Za-z]", "", line).strip()
    return "" if line in ("", "Progress:") else line[:200]


def install_status() -> dict:
    return {k: (v[-12:] if k == "lines" else v) for k, v in _INSTALL.items()}


def install_editor() -> dict:
    """Start `Unity Hub -- --headless install` for 2022.3.62f2 in the background (a 3-4 GB download)."""
    hub = paths.unity_hub()
    if not hub:
        raise RuntimeError("Unity Hub is not installed.")
    if _INSTALL["running"]:
        return install_status()
    _INSTALL.update(running=True, lines=[], ok=None, started=time.time())

    def work():
        cmd = [str(hub), "--", "--headless", "install", "--version", paths.UNITY_VERSION,
               "--changeset", paths.UNITY_CHANGESET]
        log = paths.LOGS / "unity-install.log"
        try:
            with open(log, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                for line in p.stdout:
                    f.write(line)
                    line = hub_line(line)
                    if line:
                        _INSTALL["lines"].append(line)
                        del _INSTALL["lines"][:-50]
                p.wait()
            found = paths.unity_editor(deep=True)
            ok = found is not None
            if ok:
                paths.save_config({"unity_editor": found})
            _INSTALL.update(ok=ok)
        except OSError as e:
            _INSTALL["lines"].append(f"could not run Unity Hub: {e}")
            _INSTALL.update(ok=False)
        finally:
            _INSTALL["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return install_status()


# ---- the batch build -------------------------------------------------------------------------------

def prepare_project() -> Path:
    """Copy the builder scripts into the working project (Library and settings stay between builds)."""
    for rel in ("Assets/Editor", "Packages"):
        src = paths.UNITY_TEMPLATE / rel
        dst = PROJECT / rel
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file():
                shutil.copy2(f, dst / f.name)
    return PROJECT


def unity_running() -> list[dict]:
    return paths.process_running("Unity.exe")


def build_map(job, build: Path, out: Path) -> Path:
    from .build import StageError

    editor = paths.unity_editor()
    if not editor:
        raise StageError(f"Unity {paths.UNITY_VERSION} is not installed.",
                         "Open the Setup panel (top right) and press Install Unity, or install "
                         f"{paths.UNITY_VERSION} from Unity Hub > Installs.")
    if not paths.unity_licence():
        raise StageError("Unity has no licence on this PC yet.",
                         "Open Unity Hub and sign in (a free Unity account is enough; Unity Personal is free). "
                         "Then press Build again: it carries on from here.")
    avail, total = paths.memory_mb()
    if total < MIN_RAM_MB:
        raise StageError(f"This PC has {total / 1024:.0f} GB of memory; making the map file in Unity needs about 8 GB.",
                         "Copy the Open Skies folder to a PC with 8 GB or more and press Build there: "
                         "the data already made is kept and the build carries on with the Unity step.")
    # Unity locks its project, and two editors at once can run a PC out of memory: wait for any other Unity.
    while True:
        others = unity_running()
        if not others:
            break
        job.set(detail=f"Waiting for Unity to close ({len(others)} Unity window(s) open). "
                       "Save your work there and close it, or press Stop.")
        time.sleep(10)
        job.check_cancel()
    if avail < 3000:
        job.note(f"Only {avail:,} MB of memory was free when Unity started; if it fails, close other programs.")
    proj = prepare_project()
    tmp_out = out / "unity-out"
    if tmp_out.exists():
        shutil.rmtree(tmp_out, ignore_errors=True)
    tmp_out.mkdir(parents=True)
    log = paths.LOGS / f"unity-{job.mid}.log"
    log.unlink(missing_ok=True)
    cmd = [str(editor), "-batchmode", "-nographics", "-quit", "-projectPath", str(proj),
           "-executeMethod", "NOEarth.MapBuilder.Build", "-mapBuildDir", str(build), "-outDir", str(tmp_out),
           "-logFile", str(log)]
    job.log("\n== unity: " + " ".join(cmd))
    import json
    man = json.loads((build / "manifest.json").read_text(encoding="utf-8"))
    tiles = int(round(man["mapSizeX"] / 5120)) ** 2
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    job._proc = p
    seen_tiles = set()
    wrote = False
    pos = 0
    started = time.time()
    phase = "Starting Unity (the first build also sets up the project, a few minutes)"
    while p.poll() is None:
        time.sleep(3)
        if job._cancel.is_set():
            job.cancel()
            p.wait()
            job.check_cancel()
        try:
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            chunk = ""
        for m in re.finditer(r"Assets/Map/Meshes/(tile_\d+_\d+)\.asset", chunk):
            seen_tiles.add(m.group(1))
        if "Start importing Assets/Map/" in chunk and not seen_tiles:
            phase = "Importing the textures"
        if seen_tiles:
            phase = f"Ground tiles {len(seen_tiles):,} of {tiles:,}"
        if re.search(r"Building Asset Bundle|BuildAssetBundles|Writing AssetBundle", chunk):
            phase = "Packing the map file"
        frac = 0.1 + 0.75 * len(seen_tiles) / max(tiles, 1) if seen_tiles else min(0.1, (time.time() - started) / 1800)
        wrote = wrote or "NOEARTH_BUILD_OK" in chunk
        if wrote:
            phase = "Map file written; finishing"
        if phase.startswith(("Packing", "Map file written")):
            frac = max(frac, 0.88 if phase.startswith("Packing") else 0.95)
        job.progress("unity", frac)
        job.set(detail=f"{phase}  ({(time.time() - started) / 60:.0f} min)")
        if time.time() - started > 3 * 3600:
            job.cancel()
            raise StageError("Unity took more than three hours and was stopped.",
                             "Press Build again. If it happens again, send logs/" + log.name + " with a bug report.")
    job._proc = None
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    shutil.copy2(log, paths.LOGS / f"unity-{job.mid}-last.log") if log.exists() else None
    nomap = tmp_out / f"{job.mid}.nomap"
    if p.returncode == 0 and "NOEARTH_BUILD_OK" in text and nomap.exists():
        final = out / nomap.name
        final.unlink(missing_ok=True)
        nomap.replace(final)
        shutil.rmtree(tmp_out, ignore_errors=True)
        return final
    raise explain_failure(p.returncode, text, log)


def explain_failure(code, text, log):
    from .build import StageError
    tail = "\n".join(l for l in text.splitlines()[-80:])
    low = text.lower()
    if re.search(r"no valid unity editor license|license is not valid|licens.*(not|no) .*(found|activated)|"
                 r"entitlement.*(fail|not)|unity editor is not activated", low):
        return StageError("Unity would not start: no valid licence.",
                          "Open Unity Hub, sign in, and check Preferences > Licenses shows a Personal licence. "
                          "Then press Build again.", tail)
    if re.search(r"another unity instance is running|multiple unity instances|project is already open", low):
        return StageError("Unity says the builder project is already open.",
                          "Close every Unity window and press Build again.", tail)
    if re.search(r"error cs\d+", low):
        return StageError("The map builder scripts did not compile in Unity.",
                          f"Check that Unity {paths.UNITY_VERSION} is the version being used, then press Build "
                          "again. If it fails again, send logs/" + log.name + " with a bug report.", tail)
    if re.search(r"out of memory|could not allocate memory|failed to allocate", low):
        return StageError("Unity ran out of memory.",
                          "Close other programs (browsers and games use the most) and press Build again.", tail)
    if re.search(r"no space left|not enough space|disk full", low):
        return StageError("The disk filled up while Unity was packing the map.",
                          "Free some space (the map file alone is 1 to 3 GB) and press Build again.", tail)
    m = re.search(r"(\w*Exception: [^\n]{0,200})", text)
    return StageError("Unity stopped without making the map file" + (f": {m.group(1)}" if m else "."),
                      "Press Build again. If it fails the same way, send logs/" + log.name + " with a bug report.",
                      tail)
