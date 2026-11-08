"""Open Skies: real places as Nuclear Option maps, in one window.

    choose an area  ->  pick airbases  ->  Build  ->  Install

A thin shell: it starts the local web server on a free loopback port and shows the page in a native window
(Edge WebView2 through pywebview). Without WebView2 it opens the page in the default browser instead.

    pythonw openskies.py            (or double-click OpenSkies.bat)
    python openskies.py --serve [--port N]     server only, no window (prints the address)
    python openskies.py --browser              use the default browser instead of a window
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
TITLE = "Open Skies - real places for Nuclear Option"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int) -> None:
    from app.server import app
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)


def wait_for(port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    import app.server  # noqa: F401 - fail here, where it can be reported, not in the thread
    from app import updates
    updates.start_background_check()      # at most one GitHub request a day, in the background, silent offline

    port = a.port or free_port()
    url = f"http://127.0.0.1:{port}/"
    if a.serve:
        print(f"Open Skies is serving {url}", flush=True)
        serve(port)
        return 0
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    if not wait_for(port):
        raise RuntimeError("the local server did not start within 30 seconds")
    if not a.browser:
        try:
            import webview
            webview.create_window(TITLE, url, width=1440, height=920, min_size=(1000, 680), background_color="#000000")
            # WebView2 keeps its profile here, not in %LOCALAPPDATA% (some PCs have a nearly full C: drive).
            webview.start(private_mode=False, storage_path=str(BASE / "cache" / "webview"))
            return 0
        except Exception:  # noqa: BLE001 - no WebView2: the browser does the same job
            pass
    import webbrowser
    webbrowser.open(url)
    while True:            # the page's Quit button ends the process
        time.sleep(3600)


def report_crash() -> None:
    import traceback
    where = BASE / "logs" / "openskies-crash.log"
    where.parent.mkdir(exist_ok=True)
    where.write_text(traceback.format_exc(), encoding="utf-8")
    text = (f"Open Skies could not start:\n\n{sys.exc_info()[1]}\n\nDetails were saved to {where}.\n"
            "Running Setup.bat again fixes most problems.")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Open Skies", 0x10)
    except Exception:  # noqa: BLE001
        print(text, file=sys.stderr)


if __name__ == "__main__":
    os.chdir(BASE)
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        report_crash()
        raise SystemExit(1)
