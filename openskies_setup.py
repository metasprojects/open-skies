"""Open Skies setup: everything a fresh PC needs to build and install maps, in one go.

    Setup.bat           (or: .venv\\Scripts\\python openskies_setup.py [--unattended] [--no-unity-install])

Safe to run again: each step checks first and skips what is done.
  1. checks the Python packages work
  2. downloads the airport list (OurAirports)
  3. finds Steam and Nuclear Option
  4. installs BepInEx 5 and NOCustomMaps into the game if they are missing (pinned, fingerprint-checked)
  5. checks Unity Hub, and that you have signed in to it (the one step Unity makes you do by hand)
  6. installs Unity 2022.3.62f2 through Unity Hub's command line once you have signed in
Anything still missing is listed at the end with exactly how to fix it; the app shows the same list.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, title: str) -> None:
    say()
    say(f"[{n}/6] {title}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unattended", action="store_true", help="never ask anything")
    ap.add_argument("--no-unity-install", action="store_true", help="do not install the Unity editor")
    ap.add_argument("--no-mods", action="store_true", help="do not install BepInEx or NOCustomMaps")
    a = ap.parse_args()

    step(1, "Python packages")
    import duckdb, flask, numpy, pyproj, rasterio, requests, scipy, shapely, skimage  # noqa: E401,F401
    from PIL import Image  # noqa: F401
    say(f"      ok (Python {sys.version.split()[0]}, numpy {numpy.__version__}, rasterio {rasterio.__version__}, "
        f"duckdb {duckdb.__version__})")
    from app import downloads, install, paths, setup_checks

    step(2, "Airport list (OurAirports)")
    d = downloads.ourairports()
    from app import airports
    say(f"      {len(airports.index()):,} airports with usable runways ({d})")

    step(3, "Nuclear Option")
    game = paths.game_dir()
    if game:
        say(f"      found at {game}")
        paths.save_config({"game_dir": game})
    else:
        say("      not found in your Steam libraries.")
        if not a.unattended:
            while True:
                ans = input("      Paste the Nuclear Option folder (or press Enter to skip): ").strip().strip('"')
                if not ans:
                    break
                if (Path(ans) / paths.GAME_EXE).exists():
                    paths.save_config({"game_dir": ans})
                    game = Path(ans)
                    break
                say(f"      That folder has no {paths.GAME_EXE} - try again.")

    step(4, "BepInEx and NOCustomMaps (the mod loader and the map loader)")
    if not game:
        say("      skipped - no game folder.")
    elif paths.bepinex_ok(game) and paths.nocustommaps_dll(game):
        say(f"      both installed ({paths.nocustommaps_dll(game)})")
    elif a.no_mods:
        say("      missing; not installed (--no-mods).")
    else:
        try:
            for line in install.install_mods():
                say("      " + line)
        except install.InstallError as e:
            say(f"      {e.title} {e.fix}")
        except downloads.DownloadError as e:
            say(f"      could not download: {e}")

    step(5, "Unity Hub")
    hub = paths.unity_hub()
    say(f"      found at {hub}" if hub else "      not installed.")
    licence = paths.unity_licence()
    if hub:
        say("      signed in (a Unity licence is active)" if licence else
            "      not signed in yet: open Unity Hub and sign in with a free Unity account.")

    step(6, f"Unity {paths.UNITY_VERSION}")
    editor = paths.unity_editor(deep=True)
    if editor:
        say(f"      found at {editor}")
        paths.save_config({"unity_editor": editor})
    elif not hub or not licence:
        say("      not installed; it is installed here once Unity Hub is installed and signed in.")
    elif a.no_unity_install:
        say("      not installed (--no-unity-install).")
    else:
        say("      installing through Unity Hub (about 4 GB; 10-30 minutes, progress below)...")
        cmd = [str(hub), "--", "--headless", "install", "--version", paths.UNITY_VERSION,
               "--changeset", paths.UNITY_CHANGESET]
        log = paths.LOGS / "unity-install.log"
        with open(log, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
            from app.unity import hub_line
            last = ""
            for line in p.stdout:
                f.write(line)
                line = hub_line(line)
                # one line per 10 % of a download, not one per percent
                key = re.sub(r"(\d+)\.\d+%", lambda m: f"{int(m.group(1)) // 10 * 10}%", line)
                if line and key != last:
                    say("      " + line[:150])
                    last = key
            p.wait()
        editor = paths.unity_editor(deep=True)
        say(f"      installed at {editor}" if editor else
            f"      Unity Hub finished without installing it (log: {log}).")
        if editor:
            paths.save_config({"unity_editor": editor})

    c = setup_checks.checks()
    say()
    missing = [i for i in c["items"] if not i["ok"]]
    if not missing:
        say("Setup complete. Start Open Skies by double-clicking OpenSkies.bat.")
        say("SETUP_COMPLETE")
    else:
        say("Setup incomplete. Still to do (Open Skies shows the same list, with buttons):")
        for i in missing:
            say(f"  - {i['label']}: {i['fix']}")
        say("You can already start Open Skies (OpenSkies.bat) and choose your area.")
        say("SETUP_INCOMPLETE")
    for n in c["notes"]:
        say(f"Note: {n}")
    return 0


class _Tee:
    """Everything setup prints also goes to logs/setup.log: a failed setup on someone else's PC leaves a trace."""

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, text):
        if self.stream is not None:
            self.stream.write(text)
        self.log.write(text)
        self.log.flush()

    def flush(self):
        if self.stream is not None:
            self.stream.flush()


if __name__ == "__main__":
    import traceback
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(errors="replace")
    (BASE / "logs").mkdir(exist_ok=True)
    _log = open(BASE / "logs" / "setup.log", "a", encoding="utf-8", errors="replace")
    _log.write(f"\n===== setup {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    sys.stdout = _Tee(sys.stdout, _log)
    sys.stderr = _Tee(sys.stderr, _log)
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        print("\nSetup failed. The details are in logs/setup.log. Running Setup.bat again carries on where it stopped.")
        raise SystemExit(1)
