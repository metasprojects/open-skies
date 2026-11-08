"""Report a problem: one zip a closed-beta tester sends to whoever gave them Open Skies.

In it: the logs/*.log files (newest first, about 20 MB at most), the setup check, the current or last build's
status, the app version, the Windows version, free memory and free disk. In every file the Windows user name
becomes <user>, the home folder %USERPROFILE% and the computer name <pc>. No map data, no downloads, and nothing
from config.json but the names of the settings that are set.
"""
from __future__ import annotations

import json
import os
import platform
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import build, install, paths, setup_checks, unity

LOG_CAP = 20 * 2**20          # bytes of logs per report, newest first
MIN_TAIL = 64 * 1024          # a log that doesn't fit is cut to its end, if at least this much room is left


# ---- taking the user name and home folder out ----------------------------------------------------

def make_scrubber(homes=(), users=(), pcs=()) -> Callable[[str], str]:
    """A function that replaces each home folder with %USERPROFILE%, each user name with <user> and each computer
    name with <pc>, ignoring case. A home folder matches with any mix of separators (\\, \\\\ as in JSON, /);
    names match as whole words only."""
    rules = []
    seen = {}
    for h in homes:
        if h and h.strip():
            seen.setdefault(h.strip().rstrip("\\/").lower(), h.strip().rstrip("\\/"))
    for h in sorted(seen.values(), key=len, reverse=True):
        parts = [p for p in re.split(r"[\\/]+", h) if p]
        if len(parts) < 2:                      # never treat a bare drive or a single word as a home folder
            continue
        for form in {tuple(parts), tuple(p.replace(" ", "%20") for p in parts)}:
            rules.append((re.compile(r"[\\/]+".join(map(re.escape, form)) + r"(?!\w)", re.I), "%USERPROFILE%"))
    for words, repl in ((users, "<user>"), (pcs, "<pc>")):
        uniq = {w.strip().lower(): w.strip() for w in words if w and len(w.strip()) >= 2}
        for w in sorted(uniq.values(), key=len, reverse=True):
            for form in {w, w.replace(" ", "%20")}:
                rules.append((re.compile(r"(?<!\w)" + re.escape(form) + r"(?!\w)", re.I), repl))

    def scrub(text: str) -> str:
        for rx, repl in rules:
            text = rx.sub(repl, text)
        return text
    return scrub


def _short_path(p: str) -> str:
    """The 8.3 form Windows uses in some paths (a user name with a space often shows up as NAME~1)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
        return buf.value if 0 < n < 1024 else ""
    except Exception:  # noqa: BLE001 - not Windows
        return ""


def scrubber() -> Callable[[str], str]:
    """The scrubber for this PC's user."""
    homes = [os.environ.get("USERPROFILE", ""), str(Path.home())]
    if os.environ.get("HOMEDRIVE") and os.environ.get("HOMEPATH"):
        homes.append(os.environ["HOMEDRIVE"] + os.environ["HOMEPATH"])
    homes += [_short_path(h) for h in homes if h]
    users = [os.environ.get("USERNAME", "")]
    try:
        import getpass
        users.append(getpass.getuser())
    except Exception:  # noqa: BLE001
        pass
    users += [Path(h).name for h in homes if h]            # the profile folder can differ from the account name
    return make_scrubber(homes, users, [os.environ.get("COMPUTERNAME", "")])


# ---- what goes in --------------------------------------------------------------------------------

def windows_version() -> dict:
    info = {"platform": platform.platform(), "machine": platform.machine()}
    try:
        info["edition"] = platform.win32_edition()
    except Exception:  # noqa: BLE001
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            def q(name):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return None
            product = str(q("ProductName") or "")
            number = str(q("CurrentBuildNumber") or q("CurrentBuild") or "")
            if number.isdigit() and int(number) >= 22000 and product.startswith("Windows 10"):
                product = "Windows 11" + product[len("Windows 10"):]     # Windows 11 still says 10 here
            info.update(product=product, version=q("DisplayVersion") or q("ReleaseId"),
                        build=f"{number}.{q('UBR')}" if q("UBR") is not None else number)
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
    return info


def disks() -> list[dict]:
    """Free space on the Open Skies folder's drive (and the map data's, if that is somewhere else)."""
    out = []
    for where, what in ((paths.BASE, "Open Skies folder"), (paths.DATA, "map data folder")):
        drive = where.resolve().anchor.rstrip("\\") or str(where)
        if all(d["drive"].lower() != drive.lower() for d in out):
            out.append({"what": what, "drive": drive, "freeGB": round(paths.free_gb(where), 1)})
    return out


def build_status() -> dict:
    """This session's build (running or finished), else the stages the most recent build finished."""
    job = build.current()
    out = {"current": job.status() if job else None, "copy": install.copy_status(),
           "unityInstall": unity.install_status()}
    if not job:
        stamps = sorted(paths.BUILD.glob("*/openskies-stages.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if stamps:
            s = stamps[0]
            try:
                done = json.loads(s.read_text(encoding="utf-8")).get("stages", [])
            except (OSError, ValueError):
                done = []
            mid = s.parent.name
            out["last"] = {"mapId": mid, "stagesDone": done, "stageCount": len(build.STAGES),
                           "updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.stat().st_mtime)),
                           "finishedMap": (paths.OUT / mid / f"{mid}.nomap").exists()}
    return out


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace")


def _logs(logs_dir: Path, cap: int):
    """(name, text, info) for each log, newest first, until about `cap` bytes; a log that doesn't fit is cut to
    its end. Logs left out are listed."""
    files = []
    for p in logs_dir.glob("*.log"):
        try:
            if p.is_file():
                files.append((p.stat().st_mtime, p))
        except OSError:
            pass
    files.sort(key=lambda t: t[0], reverse=True)
    left, taken, skipped = cap, [], []
    for mtime, p in files:
        info = {"name": p.name, "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))}
        try:
            size = p.stat().st_size
            if size > left and left < MIN_TAIL:
                skipped.append({**info, "sizeKB": round(size / 1024), "why": "the report was full"})
                continue
            with open(p, "rb") as f:
                head = f.read(2)
                start = max(0, size - left)
                if head in (b"\xff\xfe", b"\xfe\xff"):
                    start += start % 2                        # stay on a UTF-16 character boundary
                f.seek(start)
                data = f.read(left)
            text = _decode(head + data if start and head in (b"\xff\xfe", b"\xfe\xff") else data)
            if start:
                text = (f"[Open Skies report: the first {start:,} bytes of this log were left out to keep the report "
                        f"small]\n" + text.split("\n", 1)[-1])
            left -= len(data)
            taken.append((p.name, text, {**info, "sizeKB": round(size / 1024), "cut": bool(start)}))
        except OSError as e:
            skipped.append({**info, "why": f"could not be read ({e.__class__.__name__})"})
    return taken, skipped


README = """Open Skies problem report
=========================

Made by Open Skies' "Report a problem" button. Send this zip to whoever gave you Open Skies.

What is inside:
  report.json        Open Skies version, Windows version, free memory and disk, which settings are set
  setup-status.json  the Setup check (game, mods, Unity)
  build-status.json  the current or last map build
  logs/              the Open Skies logs, newest first (about 20 MB at most)

Your Windows user name is replaced by <user>, your home folder by %USERPROFILE% and the computer's name by <pc>.
No maps, no downloaded data, and no setting values are included.
"""


def make_report(logs_dir: Path | None = None, out_dir: Path | None = None, cap: int = LOG_CAP,
                scrub: Callable[[str], str] | None = None) -> dict:
    """Write logs/OpenSkies-report-<date-time>.zip and say what is in it."""
    logs_dir = logs_dir or paths.LOGS
    out_dir = out_dir or paths.LOGS
    scrub = scrub or scrubber()
    out_dir.mkdir(parents=True, exist_ok=True)

    def section(fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - a broken section must not stop the report
            return {"error": f"{e.__class__.__name__}: {e}"}

    taken, skipped = _logs(logs_dir, cap)
    summary = {
        "app": paths.APP_NAME, "version": paths.APP_VERSION,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "windows": section(windows_version), "python": platform.python_version(),
        "memory": section(paths.memory_detail), "disk": section(disks),
        "configKeysSet": section(lambda: sorted(paths.load_config().keys())),
        "logs": {"capMB": round(cap / 2**20, 1), "included": [t[2] for t in taken], "leftOut": skipped},
    }
    entries = [("README.txt", README),
               ("report.json", summary),
               ("setup-status.json", section(lambda: setup_checks.checks(deep=False))),
               ("build-status.json", section(build_status))]
    entries += [(f"logs/{name}", text) for name, text, _ in taken]

    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    dest = out_dir / f"OpenSkies-report-{stamp}.zip"
    part = dest.with_suffix(".zip.part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, body in entries:
            text = body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False, default=str)
            z.writestr(scrub(name), scrub(text))
    os.replace(part, dest)
    return {"file": str(dest), "name": dest.name, "sizeMB": round(dest.stat().st_size / 2**20, 1),
            "logs": len(taken), "logsLeftOut": len(skipped),
            "message": f"Saved {dest.name} in the Open Skies logs folder. Send that file to whoever gave you "
                       "Open Skies. It holds the logs and your PC's setup, with your user name taken out; no maps, "
                       "downloads or settings."}
