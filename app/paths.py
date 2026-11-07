"""Where everything lives: this app's folders, the player's Steam, game, BepInEx, NOCustomMaps and Unity.

Nothing here writes outside the Open Skies folder except config.json inside it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
PIPELINE = BASE / "pipeline"
UNITY_TEMPLATE = BASE / "unity" / "OpenSkiesBuilder"
# All downloads, map builds and finished maps. Kept inside the Open Skies folder (portable, no C: clutter) unless
# OPENSKIES_DATA points somewhere else.
DATA = Path(os.environ.get("OPENSKIES_DATA") or (BASE / "data"))
CACHE = DATA / "cache"          # downloads shared by every map
MAPS = DATA / "maps"            # one map.json per map
BUILD = DATA / "build"          # pipeline output per map
OUT = DATA / "out"              # finished .nomap, mission, credits per map
LOGS = BASE / "logs"
CONFIG = BASE / "config.json"

APP_NAME = "Open Skies"
APP_VERSION = "0.1.0-beta.1"
GAME_APPID = "2168680"
GAME_EXE = "NuclearOption.exe"
UNITY_VERSION = "2022.3.62f2"
UNITY_CHANGESET = "7670c08855a9"
USER_AGENT = f"OpenSkies/{APP_VERSION} (Nuclear Option map maker; local desktop app)"
# The notice the Nuclear Option EULA (19 Sept 2026) asks every mod to carry, verbatim.
SHOCKFRONT_NOTICE = ('This project is an unofficial community modification and is not affiliated with, sponsored by, or en'
    'dorsed by Shockfront Studios Pty Ltd. Original Nuclear Option assets, vehicle designs, audio, and co'
    'de are Copyright (c) 2026 Shockfront Studios Pty Ltd. All rights reserved. Nuclear Option and Shockf'
    'ront Studios are trademarks or registered trademarks of Shockfront Studios Pty Ltd. Original mod con'
    'tent and all other trademarks belong to their respective owners.')

for d in (CACHE, MAPS, BUILD, OUT, LOGS):
    d.mkdir(parents=True, exist_ok=True)


# ---- config ---------------------------------------------------------------------------------------

def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def save_config(update: dict) -> dict:
    cfg = load_config()
    for k, v in update.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = str(v) if isinstance(v, Path) else v
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


# ---- Steam and the game ---------------------------------------------------------------------------

def steam_dir() -> Path | None:
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            p = Path(winreg.QueryValueEx(k, name)[0])
                            if p.exists():
                                return p
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    for p in (Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam")):
        if p.exists():
            return p
    return None


def steam_libraries() -> list[Path]:
    steam = steam_dir()
    libs = []
    if steam:
        libs.append(steam)
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
    out = []
    for lib in libs:
        if lib.exists() and lib not in out:
            out.append(lib)
    return out


def _is_game(p: Path) -> bool:
    return (p / GAME_EXE).exists() and (p / "NuclearOption_Data").is_dir()


def game_dir() -> Path | None:
    """The Nuclear Option folder: from config (if still valid), else from Steam's app manifest."""
    saved = load_config().get("game_dir")
    if saved and _is_game(Path(saved)):
        return Path(saved)
    for lib in steam_libraries():
        acf = lib / "steamapps" / f"appmanifest_{GAME_APPID}.acf"
        if acf.exists():
            m = re.search(r'"installdir"\s+"([^"]+)"', acf.read_text(encoding="utf-8", errors="replace"))
            if m:
                p = lib / "steamapps" / "common" / m.group(1)
                if _is_game(p):
                    return p
    return None


def game_build_hash(game: Path | None) -> str:
    try:
        return (game / "build-hash.txt").read_text(encoding="utf-8").strip()[:40] if game else ""
    except OSError:
        return ""


def bepinex_ok(game: Path | None) -> bool:
    return bool(game) and (game / "BepInEx" / "core" / "BepInEx.dll").exists() and (game / "winhttp.dll").exists()


def nocustommaps_dll(game: Path | None) -> Path | None:
    if not game:
        return None
    plugins = game / "BepInEx" / "plugins"
    for p in [plugins / "NOCustomMaps" / "NOCustomMaps.dll", *plugins.glob("**/NOCustomMaps.dll")]:
        if p.exists():
            return p
    return None


def maps_dir(game: Path) -> Path:
    """Where NOCustomMaps looks for maps. A folder from config wins (used for test servers)."""
    saved = load_config().get("maps_dir")
    return Path(saved) if saved else game / "BepInEx" / "plugins" / "NOCustomMaps" / "maps"


def missions_dir() -> Path:
    """The game's own missions folder (Mission Editor > Open, and Play > Missions)."""
    saved = load_config().get("missions_dir")
    if saved:
        return Path(saved)
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "LocalLow" / "Shockfront" / \
        "NuclearOption" / "Missions"


def process_running(exe_name: str) -> list[dict]:
    """Running processes with this image name: [{pid, cmd}] (command line may be empty if not readable)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" | "
             "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = []
        for line in out.stdout.splitlines():
            if "\t" in line:
                pid, cmd = line.split("\t", 1)
                if pid.strip().isdigit():
                    rows.append({"pid": int(pid), "cmd": cmd})
        return rows
    except (OSError, subprocess.SubprocessError):
        return []


# ---- Unity ----------------------------------------------------------------------------------------

def unity_hub() -> Path | None:
    saved = load_config().get("unity_hub")
    cands = [Path(saved)] if saved else []
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
        if root:
            cands += [Path(root) / "Unity Hub" / "Unity Hub.exe", Path(root) / "Programs" / "Unity Hub" / "Unity Hub.exe"]
    return next((p for p in cands if p.exists()), None)


def _hub_json(name: str):
    try:
        return json.loads((Path(os.environ.get("APPDATA", "")) / "UnityHub" / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def hub_cli_editors() -> list[Path]:
    """Editors Unity Hub knows about, from its own command line (slow: only used when the quick search fails)."""
    hub = unity_hub()
    if not hub:
        return []
    try:
        out = subprocess.run([str(hub), "--", "--headless", "editors", "--installed"], capture_output=True, text=True,
                             timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    return [Path(m.group(2).strip()) for m in re.finditer(r"^(\S+)\s.*?installed at (.+)$", out.stdout, re.M)
            if m.group(1).strip(" ,") == UNITY_VERSION]


def unity_editor(deep: bool = False) -> Path | None:
    """Unity 2022.3.62f2's Unity.exe: config, then Unity Hub's lists and install folder, then the usual places;
    with deep=True also asks Unity Hub's command line."""
    want = UNITY_VERSION
    saved = load_config().get("unity_editor")
    cands = [Path(saved)] if saved else []
    reg = _hub_json("editors-v2.json") or {}
    for e in reg.get("data", []) if isinstance(reg, dict) else []:
        if e.get("version") == want:
            cands += [Path(x) for x in e.get("location", [])]
    sec = _hub_json("secondaryInstallPath.json")
    if isinstance(sec, str) and sec:
        cands.append(Path(sec) / want / "Editor" / "Unity.exe")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cands += [Path(pf) / "Unity" / "Hub" / "Editor" / want / "Editor" / "Unity.exe",
              Path(pf) / f"Unity {want}" / "Editor" / "Unity.exe"]
    for drive in "CDEFGH":
        cands += [Path(f"{drive}:\\Unity\\{want}\\Editor\\Unity.exe"),
                  Path(f"{drive}:\\Unity\\Hub\\Editor\\{want}\\Editor\\Unity.exe"),
                  Path(f"{drive}:\\Program Files\\Unity\\Hub\\Editor\\{want}\\Editor\\Unity.exe")]
    found = next((p for p in cands if p.exists()), None)
    if not found and deep:
        found = next((p for p in hub_cli_editors() if p.exists()), None)
    return found


def unity_licence() -> bool:
    """True once Unity Hub has activated a licence on this PC (signing in to the Hub does it, free Personal)."""
    pd = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Unity"
    la = Path(os.environ.get("LOCALAPPDATA", "")) / "Unity" / "licenses"
    if (pd / "Unity_lic.ulf").exists():
        return True
    try:
        return la.is_dir() and any(la.glob("*.xml"))
    except OSError:
        return False


# ---- machine ---------------------------------------------------------------------------------------

def memory_mb() -> tuple[int, int]:
    """(available physical MB, total physical MB)."""
    import ctypes

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        # Commit headroom matters as much as free RAM: a full page file kills a process even with RAM free.
        avail = min(m.ullAvailPhys, m.ullAvailPageFile)
        return int(avail / 2**20), int(m.ullTotalPhys / 2**20)
    except Exception:  # noqa: BLE001 - not Windows
        return 4096, 8192


def free_gb(path: Path) -> float:
    import shutil
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 2**30
    except OSError:
        return 0.0
