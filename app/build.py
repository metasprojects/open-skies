"""One map build, start to finish: the map data stages, the Unity stage, the missions (Free Flight, Escalation, Strike).

Runs in a background thread of the app. The page polls status(). Every stage is a separate process (the data
stages are memory-hungry and the app has to stay small), its output goes to logs/build-<map>.log, and a stage that
fails is reported in plain words. Pressing Build again with the same settings carries on from the stage that
failed; downloads are shared by every map and kept.

Judgement calls the pipeline used to leave to a person are made here:
  - ground style: desert maps get sand instead of green ground (WorldCover bare/shrub share),
  - terrain height range for the manifest (from the real heights),
  - areas Overture has no roads or buildings for: those layers are left out, with a note,
  - sea floor: patchy soundings are filled by the pipeline; a failed download falls back to a modelled sea floor,
  - memory and disk: checked before the heavy stages, with a clear message instead of a crash,
  - one build at a time on the PC (a named mutex).
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import airports, downloads, paths, unity

MUTEX_NAME = "Global\\NOEarthMakeMap"
PY = sys.executable.replace("pythonw.exe", "python.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# key, what the page says, required, rough share of the build time
STAGES = [
    ("prepare", "Checking the area", True, 1),
    ("elevation", "Downloading elevation (Copernicus)", True, 5),
    ("landcover", "Downloading land cover (ESA WorldCover)", True, 5),
    ("overture", "Downloading roads and buildings (Overture Maps)", False, 8),
    ("ferries", "Ferry routes", False, 1),
    ("shipping", "Ship traffic data (World Bank)", False, 3),
    ("seafloor", "Sea floor depth", False, 1),
    ("heights", "Building the height map", True, 4),
    ("airbases", "Placing the airbases", True, 1),
    ("textures", "Painting the ground", True, 5),
    ("buildings", "Buildings and towns", False, 3),
    ("roads", "Road network", False, 8),
    ("towns", "Towns", False, 2),
    ("trees", "Forests", False, 2),
    ("apron", "Airfield aprons", True, 1),
    ("idents", "Runway numbers", True, 1),
    ("sealanes", "Ship lanes", False, 3),
    ("manifest", "Map details and credits", True, 1),
    ("unity", "Making the map file in Unity", True, 35),
    ("mission", "Missions", True, 1),
]
LABEL = {k: label for k, label, _, _ in STAGES}
HINT = {
    "elevation": "About 20 to 40 tiles of 1 x 1 degree for a full-size map, 5 to 40 MB each. Kept for next time.",
    "landcover": "One to four 100-200 MB tiles. Kept for next time.",
    "overture": "Read straight from Overture's cloud copy of OpenStreetMap: 5 to 30 minutes, quiet while it works.",
    "shipping": "A one-off download of about 500 MB, shared by every map.",
    "heights": "A few minutes.",
    "textures": "A few minutes.",
    "buildings": "A few minutes; longer for big cities.",
    "roads": "The slowest data step: 5 to 20 minutes for a full-size map.",
    "towns": "About a minute: the real buildings become the game's own houses and shops.",
    "trees": "About a minute: forests from the land cover.",
    "sealanes": "A few minutes.",
    "unity": "Unity packs the map file: about 10 minutes on a fast PC, longer the first time.",
}
WEIGHT = {k: w for k, _, _, w in STAGES}
OPTIONAL_OUTPUTS = {
    "towns": ["cities.bytes", "cities_preview.png", "cities_preview_main.png"],
    "trees": ["trees.bytes", "trees_preview.png"],
    "buildings": ["buildings.npz", "zoning.png"],
    "roads": ["roads.bytes", "roads_draw.json", "roads_stats.json", "roads_profile.npy", "roads_profile_ground.npy",
              "roads_preview.png"],
    "sealanes": ["sealanes.bytes", "sealanes.json", "sealanes_preview.png", "sealanes_thumb.png"],
    "seafloor": ["bathy.npy"],
}
TAG_COLOURS = ["#c8102e", "#1f5fa8", "#2e7d32", "#ef6c00", "#6a1b9a", "#00838f", "#ad1457", "#5d4037"]


class StageError(Exception):
    def __init__(self, title, fix="", details=""):
        super().__init__(title)
        self.title, self.fix, self.details = title, fix, details


class Cancelled(Exception):
    pass


# ---- the map's settings -----------------------------------------------------------------------------

def sanitise_id(name: str) -> str:
    """NOCustomMaps' own mapId rule (MapIdentity.Sanitise): a-z, 0-9 and single hyphens, 2-32 characters."""
    s = re.sub(r"[^a-z0-9]+", "-", airports.ascii_name(name, 80).lower()).strip("-")
    s = s[:32].rstrip("-")
    return s if len(s) >= 2 else "open-skies-map"


def display_name(name: str) -> str:
    """Lobby-safe map name: printable ASCII, no angle brackets, at most 40 characters (MapIdentity)."""
    s = airports.ascii_name(name, 40)
    return s or "Open Skies map"


def make_config(req: dict, picked: list[dict], game_hash: str) -> dict:
    name = display_name(req["name"])
    mid = sanitise_id(name)
    tiles = int(req.get("tiles", 51))
    colour = TAG_COLOURS[int(hashlib.sha1(mid.encode()).hexdigest(), 16) % len(TAG_COLOURS)]
    must = {a["short"]: [a["lat"], a["lon"]] for a in picked}
    main = max(picked, key=lambda a: a["runwayFt"])
    cam = round(min(20000.0, tiles * 5120 / 2 * 0.6))      # the menu camera, kept well inside small maps
    return {
        "mapId": mid, "displayName": name, "version": "1.0.0",
        "author": airports.ascii_name(req.get("author") or "Open Skies", 40) or "Open Skies",
        "tagColor": colour, "builtAgainstGameHash": game_hash, "terrainHeightMax": 4500,
        "camera": {"x": -cam, "y": 3000, "z": cam, "pitch": 8, "yaw": 110, "roll": 0},
        "center": {"lat": round(float(req["lat"]), 5), "lon": round(float(req["lon"]), 5)},
        "tileMetres": 5120, "tilesPerSide": tiles, "vertexPitchMetres": 51.2, "horizontalScale": 1.0,
        "edgeFadeMetres": 9000,
        "factions": {"Boscali": "Boscali", "Primeva": "Primeva"},
        "airbases": [{"ident": a["ident"], "name": a["name"], "short": a["short"], "faction": a["side"]} for a in picked],
        "mustInclude": must,
        "closeups": {"main": [main["lat"], main["lon"]]},
        "openSkies": {"shipLanes": bool(req.get("shipLanes", True)), "app": paths.APP_VERSION},
    }


def area_problems(lat: float, lon: float, tiles: int) -> list[str]:
    probs = []
    if not 11 <= tiles <= 51:
        probs.append("The map must be between 11 and 51 tiles across (56 to 261 km). "
                     "Bigger maps do not fit: the loader's limit is 400 km and Open Skies builds up to 261 km.")
    (s, w, n, e), _ = airports.area_bounds(lat, lon, tiles)
    if n > 82 or s < -82:
        probs.append("Too close to a pole: Open Skies makes maps between 82 degrees south and north.")
    if e - w > 180 or w < -180 or e > 180:
        probs.append("The area crosses the 180th meridian. Move it a little east or west.")
    return probs


# ---- the named mutex: one map build at a time --------------------------------------------

class BuildLock:
    def __init__(self):
        self.h = None

    def acquire(self, job) -> None:
        k = ctypes.windll.kernel32
        k.CreateMutexW.restype = ctypes.c_void_p
        self.h = k.CreateMutexW(None, False, MUTEX_NAME)
        if not self.h:
            raise StageError("Could not take the build lock.", "Restart Open Skies and try again.")
        while True:
            r = k.WaitForSingleObject(ctypes.c_void_p(self.h), 3000)
            if r in (0, 0x80):          # signalled, or abandoned by a build that died: ours now
                return
            job.set(detail="Waiting for another map build on this PC to finish (one at a time)...")
            job.check_cancel()

    def release(self) -> None:
        if self.h:
            k = ctypes.windll.kernel32
            k.ReleaseMutex(ctypes.c_void_p(self.h))
            k.CloseHandle(ctypes.c_void_p(self.h))
            self.h = None


# ---- the job ---------------------------------------------------------------------------------------

class Job:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mid = cfg["mapId"]
        self.state = {"mapId": self.mid, "name": cfg["displayName"], "running": True, "done": False, "error": None,
                      "stage": "prepare", "label": LABEL["prepare"], "stageIndex": 0, "stageCount": len(STAGES),
                      "percent": 0.0, "detail": "", "notes": [], "started": time.time(), "finished": None,
                      "skipped": [], "result": None}
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._proc = None
        self.log_path = paths.LOGS / f"build-{self.mid}.log"
        self.map_dir = paths.MAPS / self.mid
        self.cfg_path = self.map_dir / "map.json"
        self.build = paths.BUILD / self.mid
        self.out = paths.OUT / self.mid
        key = hashlib.sha1(json.dumps([cfg["center"], cfg["tilesPerSide"]]).encode()).hexdigest()[:8]
        self.ov = paths.CACHE / "overture" / f"{self.mid}-{key}"
        self.work = paths.DATA / "work" / self.mid
        self.thread = threading.Thread(target=self.run, daemon=True, name=f"build-{self.mid}")

    # -- state for the page
    def set(self, **kw):
        with self._lock:
            self.state.update(kw)

    def note(self, text):
        with self._lock:
            if text not in self.state["notes"]:
                self.state["notes"].append(text)
        self.log(f"NOTE: {text}")

    def status(self):
        with self._lock:
            s = json.loads(json.dumps(self.state))
        s["elapsed"] = round((s["finished"] or time.time()) - s["started"])
        return s

    def cancel(self):
        self._cancel.set()
        p = self._proc
        if p and p.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=NO_WINDOW)

    def check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    def log(self, line):
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line.rstrip("\n") + "\n")

    def progress(self, stage, frac):
        done = sum(WEIGHT[k] for k, *_ in STAGES[:[k for k, *_ in STAGES].index(stage)])
        total = sum(WEIGHT.values())
        self.set(percent=round(100 * (done + WEIGHT[stage] * max(0.0, min(frac, 1.0))) / total, 1))

    # -- running stages
    def env(self):
        e = dict(os.environ)
        avail, _ = paths.memory_mb()
        e.update({
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
            # numpy/scipy thread pools reserve ~250 MB each at import; one thread keeps every stage small.
            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
            # DuckDB's memory cap: 600 MB on a small or busy PC, more when there is room.
            "NOEARTH_DUCKDB_MEM": os.environ.get("NOEARTH_DUCKDB_MEM") or ("1500MB" if avail > 8000 else "600MB"),
            "NOEARTH_DUCKDB_DIR": str(paths.CACHE / "duckdb"),
            "NOEARTH_OVERTURE_RELEASE": self.state.get("overtureRelease") or downloads.OVERTURE_PINNED,
            "GDAL_CACHEMAX": "256", "TEMP": str(paths.CACHE / "temp"), "TMP": str(paths.CACHE / "temp"),
        })
        return e

    def run_tool(self, stage, args, sub=None, before_retry=None):
        """Run one pipeline tool; its output goes to the log and its last line to the page. A tool that runs out of
        memory gets one more try once memory is free again (every stage can be re-run from the start)."""
        try:
            return self._run_tool(stage, args, sub)
        except StageError as e:
            if not e.title.startswith("Ran out of memory"):
                raise
            self.log(f"NOTE: {stage} ran out of memory; waiting for free memory and trying once more")
            self.set(detail="Ran low on memory; waiting a moment and trying this step again...")
            time.sleep(20)
            self.mem_check(stage, 1200)
            if before_retry:
                before_retry()
            return self._run_tool(stage, args, sub)

    def _run_tool(self, stage, args, sub=None):
        self.check_cancel()
        (paths.CACHE / "temp").mkdir(parents=True, exist_ok=True)
        cmd = [PY, "-u", *[str(a) for a in args]]
        self.log(f"\n== {stage}: {' '.join(Path(a).name if i == 2 else str(a) for i, a in enumerate(cmd))}")
        tail = []
        self._proc = subprocess.Popen(cmd, cwd=paths.PIPELINE, env=self.env(), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                      creationflags=NO_WINDOW)
        for line in self._proc.stdout:
            line = line.rstrip()
            self.log(line)
            tail.append(line)
            del tail[:-60]
            if line.strip():
                self.set(detail=_plain_line(line))
                if sub:
                    sub(line)
        code = self._proc.wait()
        self._proc = None
        self.check_cancel()
        if code:
            raise _explain(stage, code, tail)
        return tail

    def mem_check(self, stage, need_mb):
        for _ in range(40):
            avail, _ = paths.memory_mb()
            if avail >= need_mb:
                return
            self.set(detail=f"Waiting for free memory ({avail:,} MB free, this step needs about {need_mb:,} MB). "
                            "Closing other programs helps.")
            time.sleep(3)
            self.check_cancel()
        avail, _ = paths.memory_mb()
        raise StageError(f"Not enough free memory for '{LABEL[stage]}' ({avail:,} MB free, needs about {need_mb:,} MB).",
                         "Close other programs (browsers and games use the most), then press Build again. "
                         "It carries on from this step.")

    # -- the build
    def run(self):
        lock = BuildLock()
        locked = False
        try:
            self.prepare()
            self.set(detail="Waiting for the build lock...")
            lock.acquire(self)
            locked = True
            self.data_stages()
            lock.release()
            locked = False
            self.unity_stage()
            self.mission_stage()
            self.set(stage="done", label="Finished", percent=100.0, detail="Your map is ready. Press Install.",
                     done=True, result=self.result())
        except Cancelled:
            self.set(error={"title": "Build stopped.", "fix": "Press Build to carry on from where it stopped.",
                            "details": ""}, detail="Stopped.")
        except StageError as e:
            self.log(f"ERROR: {e.title}\n{e.details}")
            self.set(error={"title": e.title, "fix": e.fix, "details": e.details[-4000:], "stage": self.state["stage"]})
        except Exception as e:  # noqa: BLE001 - anything else still gets a readable message
            import traceback
            tb = traceback.format_exc()
            self.log(tb)
            self.set(error={"title": f"Something went wrong during '{self.state['label']}'.",
                            "fix": "Press Build to try again. If it happens again, send logs/"
                                   f"{self.log_path.name} with your bug report.",
                            "details": tb[-4000:], "stage": self.state["stage"]})
        finally:
            if locked:
                lock.release()
            self.set(running=False, finished=time.time())

    def stamp_path(self):
        return self.build / "openskies-stages.json"

    def done_stages(self):
        try:
            s = json.loads(self.stamp_path().read_text(encoding="utf-8"))
            return s["stages"] if s.get("config") == self.cfg_hash else []
        except (OSError, ValueError, KeyError):
            return []

    def forget_from(self, stage):
        keys = [k for k, *_ in STAGES]
        keep = [k for k in self.done_stages() if keys.index(k) < keys.index(stage)]
        self.stamp_path().write_text(json.dumps({"config": self.cfg_hash, "stages": keep}), encoding="utf-8")

    def mark(self, stage):
        done = self.done_stages()
        if stage not in done:
            done.append(stage)
        self.stamp_path().write_text(json.dumps({"config": self.cfg_hash, "stages": done}), encoding="utf-8")

    def stage(self, key):
        i = [k for k, *_ in STAGES].index(key)
        self.set(stage=key, label=LABEL[key], stageIndex=i, detail="", hint=HINT.get(key, ""))
        self.progress(key, 0)

    def prepare(self):
        self.stage("prepare")
        self.log(f"\n===== Open Skies {paths.APP_VERSION} build of {self.mid} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.map_dir.mkdir(parents=True, exist_ok=True)
        new = json.dumps({k: v for k, v in self.cfg.items() if k not in ("terrainHeightMax", "bareGround")},
                         sort_keys=True)
        self.cfg_hash = hashlib.sha1(new.encode()).hexdigest()
        self.resuming = True
        if self.done_stages() == [] and self.build.exists():
            shutil.rmtree(self.build, ignore_errors=True)      # a different area or airport list: start clean
        self.build.mkdir(parents=True, exist_ok=True)
        old = {}
        if self.cfg_path.exists():
            try:
                old = json.loads(self.cfg_path.read_text(encoding="utf-8"))
            except ValueError:
                old = {}
        for k in ("terrainHeightMax", "bareGround"):          # decided by earlier stages of this same build
            if k in old and self.done_stages():
                self.cfg[k] = old[k]
        self.save_cfg()
        need = 14.0 + (0 if downloads.shipping_ready() or not self.cfg["openSkies"]["shipLanes"] else 0.6)
        free = paths.free_gb(paths.DATA)
        if free < need:
            raise StageError(f"Not enough disk space: {free:.1f} GB free where Open Skies keeps its data, "
                             f"a map needs about {need:.0f} GB while it builds.",
                             f"Free some space on {paths.DATA.anchor} or move the Open Skies folder to a bigger drive.")
        self.set(overtureRelease=downloads.overture_release())
        self.log(f"Overture release: {self.state['overtureRelease']}")

    def save_cfg(self):
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")

    def data_stages(self):
        cfg, build, ov = self.cfg_path, self.build, self.ov
        cache = paths.CACHE
        done = self.done_stages()

        def run(key, fn):
            # Carry on from the first step not done, and redo every step after it: a later step may depend on what
            # an earlier one makes (roads need the Overture download, the manifest lists the roads file, ...).
            if self.resuming and key in done:
                self.log(f"== {key} (already done)")
                return
            if self.resuming:
                self.resuming = False
                self.forget_from(key)
            self.stage(key)
            if fn() is not False:       # False: a download failed; it is tried again on the next Build
                self.mark(key)

        run("prepare", lambda: self.run_tool("prepare", ["check_area.py", cfg]))
        self.mem_check("elevation", 500)
        run("elevation", lambda: self.run_tool("elevation", ["fetch_dem.py", cfg, cache / "copernicus-glo30"],
                                               sub=self._count_sub("elevation", r"tiles present|: .*MB|none \(all sea\)", 6)))
        run("landcover", self.landcover)
        run("overture", self.overture)
        run("ferries", self.ferries)
        run("shipping", self.shipping)
        run("seafloor", self.seafloor)
        self.mem_check("heights", 900)
        run("heights", self.heights)
        run("airbases", lambda: self.run_tool("airbases", ["airbases.py", cfg, cache / "ourairports", build]))
        self.mem_check("textures", 900)
        run("textures", lambda: self.run_tool("textures", ["textures.py", cfg, self.view("worldcover"), build]))
        run("buildings", self.buildings)
        run("roads", self.roads)
        run("towns", self.towns)
        run("trees", self.trees)
        run("apron", self.apron)
        run("idents", self.idents)
        run("sealanes", self.sealanes)
        run("manifest", lambda: self.run_tool("manifest", ["manifest.py", cfg, build]))

    def _count_sub(self, stage, pattern, expected):
        seen = [0]

        def sub(line):
            if re.search(pattern, line):
                seen[0] += 1
                self.progress(stage, seen[0] / max(expected, 1))
        return sub

    def view(self, kind):
        """A per-map folder of hard links to just the downloaded tiles this map covers. The pipeline reads every
        .tif in the folder it is given, so a shared cache of many maps' tiles would slow every build down."""
        d = self.work / kind
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        (s, w, n, e), _ = airports.area_bounds(self.cfg["center"]["lat"], self.cfg["center"]["lon"],
                                               self.cfg["tilesPerSide"], 5000)
        names = []
        if kind == "dem":
            src = paths.CACHE / "copernicus-glo30"
            for lat in range(math.floor(s), math.floor(n) + 1):
                for lon in range(math.floor(w), math.floor(e) + 1):
                    ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
                    names.append(f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM.tif")
        else:
            src = paths.CACHE / "esa-worldcover"
            for lat in range(math.floor(s / 3) * 3, math.floor(n / 3) * 3 + 1, 3):
                for lon in range(math.floor(w / 3) * 3, math.floor(e / 3) * 3 + 1, 3):
                    names.append(f"ESA_WorldCover_10m_2021_v200_{'N' if lat >= 0 else 'S'}{abs(lat):02d}"
                                 f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_Map.tif")
        for name in names:
            f = src / name
            if f.exists() and f.stat().st_size > 0:
                try:
                    os.link(f, d / name)
                except OSError:
                    shutil.copy2(f, d / name)      # drives without hard links (FAT/exFAT)
        return d

    def landcover(self):
        self.run_tool("landcover", ["fetch_landcover.py", self.cfg_path, paths.CACHE],
                      sub=self._count_sub("landcover", r"\.tif: |\.csv: ", 4))
        info = self.helper("cover", self.cfg_path, self.view("worldcover"))
        self.log(f"land cover: {info}")
        if info["land"] < 0.02:
            self.note("This area is almost all sea. The map will build, but there is little land to fly over.")
        if info["desert"] > 0.45:
            self.cfg["bareGround"] = "desert"
            self.save_cfg()
            self.note("Desert area: the ground is painted as sand and scrub instead of grass.")

    def helper(self, *args):
        out = subprocess.run([PY, str(paths.BASE / "app" / "stagehelpers.py"), *[str(a) for a in args]],
                             capture_output=True, text=True, env=self.env(), creationflags=NO_WINDOW, timeout=1800)
        if out.returncode:
            self.log(out.stdout + out.stderr)
            raise StageError("An internal check failed.", "Press Build to try again.", (out.stdout + out.stderr)[-3000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def clear_overture_parts(self):
        # overture.py writes each piece to <kind>.<k>.writing and renames it to .part only when complete; a
        # .writing file is what a crash leaves behind. (The pipeline clears them itself too.)
        for f in self.ov.glob("*.writing"):
            f.unlink(missing_ok=True)

    def overture(self):
        self.mem_check("overture", 900)
        self.clear_overture_parts()
        try:
            self.run_tool("overture", ["overture.py", "fetch", self.cfg_path, self.ov],
                          sub=self._count_sub("overture", r"^\s+\w+: ", 7), before_retry=self.clear_overture_parts)
        except StageError as e:
            if e.title.startswith("Ran out of memory"):
                raise
            self.note("Roads and buildings could not be downloaded from Overture Maps "
                      f"({e.title.rstrip('.')}). The map is built with terrain and airbases only.")
            self.state["skipped"] += ["overture", "ferries", "buildings", "roads"]
            self.clear_overture_parts()
            return False
        counts = self.helper("count", self.ov)
        self.log(f"overture counts: {counts}")
        self.counts = counts
        if counts.get("roads", 0) <= 0:
            self.note("Overture Maps has no roads for this area, so the map has none.")
        if counts.get("buildings", 0) <= 0:
            self.note("Overture Maps has no buildings for this area, so the map has no towns.")

    def ov_count(self, kind):
        if "overture" in self.state["skipped"]:
            return 0
        if not hasattr(self, "counts"):
            self.counts = self.helper("count", self.ov) if self.ov.exists() else {}
        return self.counts.get(kind, 0)

    def ferries(self):
        if "overture" in self.state["skipped"]:
            return
        try:
            self.run_tool("ferries", ["fetch_ferries.py", self.cfg_path, self.ov])
        except StageError as e:
            if e.title.startswith("Ran out of memory"):
                raise
            self.note("Ferry routes could not be downloaded; ship lanes use ship traffic only.")
            (self.ov / "ferries.parquet").unlink(missing_ok=True)
            return False

    def shipping(self):
        if not self.cfg["openSkies"]["shipLanes"]:
            self.state["skipped"] += ["shipping", "sealanes"]
            return
        if downloads.shipping_ready():
            return
        self.set(detail=f"Downloading {downloads.SHIPPING_MB} MB of ship traffic data (once for all maps)...")
        try:
            downloads.shipping(progress=lambda d, t: (self.progress("shipping", d / max(t, 1)),
                                                      self.set(detail=f"Ship traffic data: {d / 1e6:,.0f} of "
                                                                      f"{t / 1e6:,.0f} MB")))
        except downloads.DownloadError as e:
            self.note(f"Ship traffic data could not be downloaded ({e}); the map has no ship lanes.")
            self.state["skipped"].append("sealanes")
            return False

    def seafloor(self):
        try:
            self.run_tool("seafloor", ["fetch_bathy.py", self.cfg_path, paths.CACHE / "terrarium", self.build])
        except StageError:
            (self.build / "bathy.npy").unlink(missing_ok=True)
            self.note("Sea floor depths could not be downloaded; the sea floor is modelled from the coastline "
                      "(deepening offshore to 70 m).")
            return False

    def heights(self):
        tail = self.run_tool("heights", ["build_height.py", self.cfg_path, self.view("dem"), self.build])
        (self.build / "heights_raw.npy").unlink(missing_ok=True)
        for line in tail:
            m = re.search(r"sea floor: (\d+)% of the sea had no soundings", line)
            if m and int(m.group(1)) >= 5:
                self.note(f"{m.group(1)}% of the sea had no depth soundings (common near coasts and in fjords); "
                          "those parts take the depth of the nearest real sounding, so ships can still sail them.")
        h = self.helper("heights", self.build)
        top = max(1000, int(math.ceil((h["max"] + 500) / 500.0) * 500))
        if top != self.cfg.get("terrainHeightMax"):
            self.cfg["terrainHeightMax"] = top
            self.save_cfg()
        if h["land"] < 0.01:
            self.note("There is almost no land on this map.")

    def buildings(self):
        if self.ov_count("buildings") <= 0:
            self.skip("buildings")
            return
        self.mem_check("buildings", 900)
        try:
            self.run_tool("buildings", ["buildings.py", self.cfg_path, self.ov, self.view("worldcover"), self.build])
        except StageError as e:
            if "memory" in e.title.lower():
                raise
            self.skip("buildings", f"Buildings could not be placed ({e.title.rstrip('.')}); the map has none.")

    def roads(self):
        if self.ov_count("roads") <= 0:
            self.skip("roads")
            return
        self.mem_check("roads", 1000)
        try:
            self.run_tool("roads", ["roads.py", self.cfg_path, self.build, self.ov])
        except StageError as e:
            if "memory" in e.title.lower():
                raise
            self.skip("roads", f"The road network could not be built ({e.title.rstrip('.')}); the map has no roads.")
            return
        try:
            self.run_tool("roads", ["roads_preview.py", self.cfg_path, self.build])
        except StageError:
            pass                                         # only a picture

    def towns(self):
        if not (self.build / "buildings.npz").exists():
            self.skip("towns")
            return
        self.mem_check("towns", 900)
        try:
            self.run_tool("towns", ["cities.py", self.cfg_path, self.build])
        except StageError as e:
            if e.title.startswith("Ran out of memory"):
                raise
            self.skip("towns", f"Towns could not be placed ({e.title.rstrip('.')}); the map has no buildings.")

    def trees(self):
        self.mem_check("trees", 900)
        try:
            self.run_tool("trees", ["trees.py", self.cfg_path, self.view("worldcover"), self.build])
        except StageError as e:
            if e.title.startswith("Ran out of memory"):
                raise
            self.skip("trees", f"Forests could not be planted ({e.title.rstrip('.')}); the map has no trees.")

    def apron(self):
        sides = self.helper("apron", self.cfg_path, self.build)
        self.log(f"apron sides: {sides}")

    def idents(self):
        names = self.helper("idents", self.cfg_path, self.build, paths.CACHE / "ourairports")
        self.log(f"runway numbers: {names}")

    def sealanes(self):
        if "sealanes" in self.state["skipped"] or not self.cfg["openSkies"]["shipLanes"]                 or not downloads.shipping_ready():
            if not self.cfg["openSkies"]["shipLanes"] and "shipping" not in self.state["skipped"]:
                self.state["skipped"].append("shipping")       # also on a resumed build that skipped that step
            self.skip("sealanes")
            return
        self.mem_check("sealanes", 900)
        try:
            self.run_tool("sealanes", ["sealanes.py", self.cfg_path, self.build, paths.CACHE / "shipping", self.ov])
        except StageError as e:
            if "memory" in e.title.lower():
                raise
            self.skip("sealanes", "Ship lanes could not be worked out for this area (usually: too little open "
                                  "water or traffic); ships use the game's own routing.")

    def skip(self, stage, why=None):
        for f in OPTIONAL_OUTPUTS.get(stage, []):
            (self.build / f).unlink(missing_ok=True)
        if stage not in self.state["skipped"]:
            self.state["skipped"].append(stage)
        if why:
            self.note(why)

    # -- Unity and the mission
    def unity_stage(self):
        self.stage("unity")
        nomap = self.out / f"{self.mid}.nomap"
        if self.resuming and "unity" in self.done_stages() and nomap.exists():
            return
        if self.resuming:
            self.resuming = False
            self.forget_from("unity")
        self.out.mkdir(parents=True, exist_ok=True)
        unity.build_map(self, self.build, self.out)
        self.mark("unity")

    def mission_stage(self):
        self.stage("mission")
        nomap = self.out / f"{self.mid}.nomap"
        self.set(detail="Fingerprinting the map file (the game names it by its SHA-256)...")
        digest = downloads.file_sha256(nomap)
        prefab = f"cm.{self.mid}.{digest[:8]}"
        (self.out / "prefab.txt").write_text(prefab + "\n", encoding="utf-8")
        title = mission_title(self.cfg["displayName"])
        missions = self.out / "missions"
        if missions.exists():
            shutil.rmtree(missions, ignore_errors=True)
        tail = self.run_tool("mission", [paths.BASE / "app" / "freeflight.py", self.cfg_path, self.build, prefab,
                                         missions, title])
        for line in tail:
            if line.startswith("NOTE: "):
                self.note(line[6:])
        # Escalation and Strike (pipeline scenarios). A mission that can't be made here is left out with a note;
        # these never fail the build: Free Flight is enough to fly the map.
        self.set(detail="Making the Escalation and Strike missions...")
        try:
            tail = self.run_tool("mission", [paths.BASE / "app" / "scenario_missions.py", self.cfg_path, self.build,
                                             prefab, missions, self.cfg["displayName"]])
        except StageError as e:
            self.log(f"NOTE: the Escalation and Strike missions failed: {e.title}")
            tail = ["NOTE: Only the Free Flight mission was made: the Escalation and Strike missions failed "
                    "(the log has the details)."]
        for line in tail:
            if line.startswith("NOTE: "):
                self.note(line[6:])
        write_readme(self.cfg, self.build, self.out, prefab, title, self.state["notes"])
        self.mark("mission")

    def result(self):
        return built_map(self.mid)


def mission_title(name: str) -> str:
    t = re.sub(r'[\\/:*?"<>|]', "", f"{name} Free Flight").strip(" .")
    return t[:60]


def _plain_line(line: str) -> str:
    line = re.sub(r"^\[\s*[\d.]+s[^\]]*\]\s*", "", line.strip())
    return line[:160]


def _explain(stage, code, tail) -> StageError:
    text = "\n".join(tail)
    low = text.lower()
    what = LABEL.get(stage, stage)
    if re.search(r"memoryerror|unable to allocate|out of memory|cannot allocate|std::bad_alloc|0xc0000017", low) \
            or code in (-1073741801, 3221225495):        # STATUS_NO_MEMORY
        return StageError(f"Ran out of memory during '{what}'.",
                          "Close other programs, then press Build again. It carries on from this step.", text)
    if re.search(r"no space left|errno 28|disk full|not enough space", low):
        return StageError(f"The disk filled up during '{what}'.", "Free some space, then press Build again.", text)
    if re.search(r"connectionerror|max retries|getaddrinfo|timed out|temporary failure in name resolution|"
                 r"remote end closed|ssl|http error|httperror|could not resolve|io error: http|connection reset", low):
        return StageError(f"Could not download the data for '{what}'.",
                          "Check the internet connection, then press Build again. Finished downloads are kept.", text)
    if "permissionerror" in low or "access is denied" in low:
        return StageError(f"A file was locked during '{what}'.",
                          "Close any program that has the map's files open (image viewers, the game), "
                          "then press Build again.", text)
    return StageError(f"'{what}' failed.", "Press Build to try again; finished steps are kept. If it fails the "
                                            "same way, send the log (Show details > Open logs) with a bug report.",
                      text)


# ---- results ------------------------------------------------------------------------------------------

def built_map(mid: str) -> dict | None:
    out = paths.OUT / mid
    nomap = out / f"{mid}.nomap"
    prefab = out / "prefab.txt"
    if not nomap.exists() or not prefab.exists():
        return None
    cfg = {}
    try:
        cfg = json.loads((paths.MAPS / mid / "map.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    missions = sorted(p.name for p in (out / "missions").iterdir() if p.is_dir()) if (out / "missions").exists() else []
    previews = [f for f in ("preview.png", "roads_preview.png", "cities_preview.png", "trees_preview.png",
                            "sealanes_preview.png", "map.png")
                if (paths.BUILD / mid / f).exists()]
    return {"mapId": mid, "name": cfg.get("displayName", mid), "sizeMB": round(nomap.stat().st_size / 2**20),
            "prefab": prefab.read_text(encoding="utf-8").strip(), "missions": missions, "previews": previews,
            "built": time.strftime("%Y-%m-%d %H:%M", time.localtime(nomap.stat().st_mtime)),
            "airbases": len(cfg.get("airbases", [])), "folder": str(out)}


def write_readme(cfg, build, out, prefab, title, notes):
    man = json.loads((build / "manifest.json").read_text(encoding="utf-8"))
    ab = json.loads((build / "airbases.json").read_text(encoding="utf-8"))
    md = out / "missions"
    folders = (sorted(p.name for p in md.iterdir() if p.is_dir()) if md.exists() else []) or [title]
    lines = [
        f"{cfg['displayName']} - a real-world map for Nuclear Option, made with Open Skies {paths.APP_VERSION}",
        "",
        f"Map id {cfg['mapId']}, registered in the game as {prefab}",
        f"Centre {cfg['center']['lat']}, {cfg['center']['lon']}; {man['mapSizeX'] / 1000:.2f} km square.",
        "Airbases: " + ", ".join(f"{a['displayName']} ({a['ident']}, {a['faction']})" for a in ab),
        "",
        "Needs BepInEx 5 and NOCustomMaps 1.4 or later. Install by copying",
        f"  {cfg['mapId']}.nomap  to  <Nuclear Option>\\BepInEx\\plugins\\NOCustomMaps\\maps\\",
        *[f"  missions\\{t}\\  to  %USERPROFILE%\\AppData\\LocalLow\\Shockfront\\NuclearOption\\Missions\\"
          for t in folders],
        "(Open Skies' Install button does both.) Every player in a multiplayer game needs this exact build.",
        "",
        "Data credits", "------------", man["credits"], "", man["notice"], "",
        "Sea floor: AWS Terrain Tiles (Terrarium), from ETOPO1, GEBCO and NOAA coastal models; sources and credits:",
        "  https://github.com/tilezen/joerd/blob/master/docs/attribution.md",
        "",
        "Roads and ferries (Overture Maps transportation, ODbL): (c) OpenStreetMap contributors; TomTom.",
        "Buildings (Overture Maps buildings, ODbL): (c) OpenStreetMap contributors; Esri Community Maps contributors",
        "  (CC BY 4.0); Microsoft Global ML Building Footprints (ODbL); Google Open Buildings (CC BY 4.0); USGS 3D",
        "  Elevation Program; Qian Shi et al., buildings of East Asian countries, Zenodo 2023, doi:10.5281/zenodo.8174931",
        "  (CC BY 4.0); work derived from BTN 2024, ign.es (CC BY 4.0).",
        "Water, land use and infrastructure (Overture Maps base, ODbL): (c) OpenStreetMap contributors; Daylight Map",
        "  Distribution; (c) ESA WorldCover project 2020 / contains modified Copernicus Sentinel data (2020) processed",
        "  by ESA WorldCover consortium (CC BY 4.0); ETOPO1 (ODC PDDL); GLOBathy (CC0).",
        "Places (Overture Maps places, CDLA Permissive 2.0): data from Meta, DAC, BrightQuery, Uberall and OpenTravel",
        "  Alliance.",
        "",
        "OpenStreetMap data (ODbL): the roads, buildings, ferries and water in this map come from OpenStreetMap",
        "and the sources above through Overture Maps, under the Open Database License. Open Skies exports the",
        "exact data used for this map on request (Export OSM data), and anyone who receives the map may ask for it.",
        "",
        paths.SHOCKFRONT_NOTICE, "It contains no game files. Mods for Nuclear Option may not be sold or put behind "
        "a paywall.",
    ]
    if notes:
        lines += ["", "Build notes", "-----------", *notes]
    (out / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- one build at a time in this app -----------------------------------------------------------------

_JOB: Job | None = None
_JOB_LOCK = threading.Lock()


def start(cfg: dict) -> Job:
    global _JOB
    with _JOB_LOCK:
        if _JOB and _JOB.state["running"]:
            raise RuntimeError("A map is already being built.")
        _JOB = Job(cfg)
        _JOB.thread.start()
        return _JOB


def current() -> Job | None:
    return _JOB
