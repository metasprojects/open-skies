"""Open Skies' local web server: the page, map tiles, place search and the build/install API.

Listens on 127.0.0.1 only and refuses requests addressed to any other host name (a web page elsewhere could
otherwise point a name of its own at 127.0.0.1 and call this API).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from . import airports, build, downloads, install, paths, setup_checks, unity

app = Flask(__name__, template_folder=str(paths.BASE / "templates"), static_folder=str(paths.BASE / "static"))
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
DRAFT = paths.DATA / "draft.json"


@app.before_request
def _only_local():
    host = request.host or ""
    host = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    if host not in LOCAL_HOSTS:
        return ("Open Skies only answers requests from this computer.", 403)


@app.errorhandler(Exception)
def _error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import traceback
    with open(paths.LOGS / "app.log", "a", encoding="utf-8") as f:
        f.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} {request.path}\n{traceback.format_exc()}")
    if isinstance(e, install.InstallError):
        return jsonify({"error": {"title": e.title, "fix": e.fix}}), 400
    return jsonify({"error": {"title": f"Something went wrong: {e}", "fix": "Details are in logs/app.log."}}), 500


@app.route("/")
def index():
    return render_template("index.html", version=paths.APP_VERSION, shipping_mb=downloads.SHIPPING_MB)


# ---- map tiles, the way the OSM tile policy asks: our own User-Agent, cached at least 7 days -----------

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_CACHE = paths.CACHE / "tiles"
TILE_MIN_AGE = 7 * 24 * 3600
TILE_FETCHES = threading.BoundedSemaphore(2)


@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def tile(z, x, y):
    import requests
    if not (0 <= z <= 18 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return ("", 404)
    path = TILE_CACHE / str(z) / str(x) / f"{y}.png"
    meta = path.with_suffix(".json")
    info = {}
    if path.exists() and meta.exists():
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
    if not (path.exists() and time.time() < info.get("expires", 0)):
        headers = {"User-Agent": paths.USER_AGENT}
        if path.exists() and info.get("etag"):
            headers["If-None-Match"] = info["etag"]
        try:
            with TILE_FETCHES:
                r = requests.get(TILE_URL.format(z=z, x=x, y=y), headers=headers, timeout=20)
            if r.status_code in (200, 304):
                if r.status_code == 200:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(r.content)
                    info["etag"] = r.headers.get("ETag")
                info["expires"] = time.time() + TILE_MIN_AGE
                meta.write_text(json.dumps(info), encoding="utf-8")
            elif not path.exists():
                return ("", r.status_code)
        except requests.RequestException:
            if not path.exists():
                return ("", 502)
    resp = send_file(path, mimetype="image/png")
    resp.headers["Cache-Control"] = f"max-age={TILE_MIN_AGE}"
    return resp


# ---- place search (Nominatim: one request a second, cached, our own User-Agent) ----------------------

_SEARCH = {}
_LAST_SEARCH = [0.0]
_SEARCH_LOCK = threading.Lock()


@app.route("/api/search")
def search():
    import requests
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    key = q.lower()
    if key in _SEARCH and time.time() - _SEARCH[key][0] < 86400:
        return jsonify({"results": _SEARCH[key][1]})
    with _SEARCH_LOCK:
        wait = 1.1 - (time.time() - _LAST_SEARCH[0])
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"q": q, "format": "jsonv2", "limit": 8},
                             headers={"User-Agent": paths.USER_AGENT}, timeout=20)
            r.raise_for_status()
            rows = r.json()
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"Search is not answering ({e.__class__.__name__}). Try again, or move the "
                                     "map by hand."}), 502
        finally:
            _LAST_SEARCH[0] = time.time()
    res = [{"name": x.get("display_name", ""), "lat": float(x["lat"]), "lon": float(x["lon"]),
            "kind": x.get("type", "")} for x in rows]
    _SEARCH[key] = (time.time(), res)
    return jsonify({"results": res})


# ---- area and airports ------------------------------------------------------------------------------

@app.route("/api/area", methods=["POST"])
def area():
    b = request.get_json(force=True)
    lat, lon, tiles = float(b["lat"]), float(b["lon"]), int(b.get("tiles", 51))
    probs = build.area_problems(lat, lon, tiles)
    (s, w, n, e), corners = airports.area_bounds(lat, lon, tiles)
    rows = [] if probs else airports.in_area(lat, lon, tiles)
    return jsonify({"problems": probs, "corners": corners, "bounds": [s, w, n, e], "airports": rows,
                    "sizeKm": round(tiles * 5.12, 2)})


@app.route("/api/draft", methods=["GET", "POST"])
def draft():
    if request.method == "POST":
        DRAFT.write_text(json.dumps(request.get_json(force=True)), encoding="utf-8")
        return jsonify({"ok": True})
    try:
        return jsonify(json.loads(DRAFT.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return jsonify({})


# ---- build ------------------------------------------------------------------------------------------

@app.route("/api/build", methods=["POST"])
def start_build():
    b = request.get_json(force=True)
    lat, lon, tiles = float(b["lat"]), float(b["lon"]), int(b.get("tiles", 51))
    probs = build.area_problems(lat, lon, tiles)
    if not (b.get("name") or "").strip():
        probs.append("Give the map a name.")
    rows = airports.in_area(lat, lon, tiles) if not probs else []
    want = {a["ident"]: a.get("side", "Boscali") for a in b.get("airports", [])}
    for a in rows:
        a["selected"] = a["ident"] in want
        if a["selected"]:
            a["side"] = "Primeva" if want[a["ident"]] == "Primeva" else "Boscali"
    if not probs:
        probs += airports.check_selection(rows)
    if probs:
        return jsonify({"problems": probs}), 400
    picked = [a for a in rows if a["selected"]]
    cfg = build.make_config(b, picked, paths.game_build_hash(paths.game_dir()))
    if b.get("fresh"):
        (paths.BUILD / cfg["mapId"] / "openskies-stages.json").unlink(missing_ok=True)
    try:
        job = build.start(cfg)
    except RuntimeError as e:
        return jsonify({"problems": [str(e)]}), 409
    return jsonify(job.status())


@app.route("/api/status")
def status():
    job = build.current()
    return jsonify({"build": job.status() if job else None, "copy": install.copy_status(),
                    "unityInstall": unity.install_status()})


@app.route("/api/stop", methods=["POST"])
def stop():
    job = build.current()
    if job and job.state["running"]:
        job.cancel()
    return jsonify({"ok": True})


@app.route("/api/log")
def log_tail():
    job = build.current()
    if not job or not job.log_path.exists():
        return jsonify({"lines": []})
    lines = job.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-120:]
    return jsonify({"lines": lines, "path": str(job.log_path)})


@app.route("/api/maps")
def maps():
    out = []
    for d in sorted(paths.OUT.iterdir()) if paths.OUT.exists() else []:
        m = build.built_map(d.name)
        if m:
            m["installed"] = (d / "installed.txt").exists()
            out.append(m)
    return jsonify({"maps": out})


@app.route("/preview/<mid>/<name>")
def preview(mid, name):
    if name not in ("preview.png", "roads_preview.png", "zoning.png", "sealanes_preview.png", "map.png") or \
            build.sanitise_id(mid) != mid:
        return ("", 404)
    p = paths.BUILD / mid / name
    return send_file(p, mimetype="image/png") if p.exists() else ("", 404)


# ---- install ----------------------------------------------------------------------------------------

@app.route("/api/install", methods=["POST"])
def install_map():
    mid = request.get_json(force=True)["mapId"]
    return jsonify(install.install_map(build.sanitise_id(mid)))


@app.route("/api/export-osm", methods=["POST"])
def export_osm():
    mid = build.sanitise_id(request.get_json(force=True)["mapId"])
    z = install.export_osm(mid)
    _reveal(z)
    return jsonify({"file": str(z), "sizeMB": round(z.stat().st_size / 2**20)})


# ---- setup ------------------------------------------------------------------------------------------

@app.route("/api/setup-status")
def setup_status():
    return jsonify(setup_checks.checks(deep=request.args.get("deep") == "1"))


@app.route("/api/setup/<action>", methods=["POST"])
def setup_action(action):
    if action == "install-mods":
        return jsonify({"done": install.install_mods()})
    if action == "install-unity":
        return jsonify(unity.install_editor())
    if action == "get-hub":
        exe = downloads.fetch(downloads.UNITY_HUB_SETUP, paths.CACHE / "installers" / "UnityHubSetup-x64.exe")
        # Not pinned (Unity replaces it with every Hub release), so check it is signed by Unity before running it.
        signer = downloads.authenticode_signer(exe)
        if "Unity Technologies" not in signer:
            exe.unlink(missing_ok=True)
            return jsonify({"error": {"title": "The Unity Hub download was not signed by Unity, so it was not started.",
                                      "fix": "Download Unity Hub yourself from unity.com/download."}}), 400
        os.startfile(exe)  # noqa: S606 - Unity's own signed installer, the person clicks through it
        return jsonify({"done": ["Unity Hub's installer has started. Finish it, sign in, then press Check again."]})
    if action == "open-hub":
        hub = paths.unity_hub()
        if hub:
            subprocess.Popen([str(hub)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return jsonify({"done": ["Unity Hub is opening. Sign in there, then press Check again."]})
    if action == "pick-game":
        folder = (request.get_json(silent=True) or {}).get("folder", "").strip().strip('"')
        p = Path(folder)
        if not (p / paths.GAME_EXE).exists():
            return jsonify({"error": {"title": f"No {paths.GAME_EXE} in that folder.",
                                      "fix": "Choose the folder that holds NuclearOption.exe."}}), 400
        paths.save_config({"game_dir": p})
        return jsonify({"done": [f"Game folder set to {p}"]})
    return ("", 404)


@app.route("/api/open", methods=["POST"])
def open_folder():
    which = request.get_json(force=True).get("which")
    mid = build.sanitise_id(request.get_json(force=True).get("mapId", "x"))
    target = {"logs": paths.LOGS, "map": paths.OUT / mid, "data": paths.DATA}.get(which)
    if target and target.exists():
        os.startfile(target)  # noqa: S606
    return jsonify({"ok": True})


def _reveal(p: Path):
    try:
        subprocess.Popen(["explorer", "/select,", str(p)])
    except OSError:
        pass


@app.route("/api/quit", methods=["POST"])
def quit_app():
    job = build.current()
    if job and job.state["running"]:
        return jsonify({"error": {"title": "A map is still building.", "fix": "Stop it first, or leave Open Skies open."}}), 409
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify({"ok": True})
