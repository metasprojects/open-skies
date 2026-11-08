"""Tell the person when a newer Open Skies is out.

At most once a day, in a background thread, one plain GET of the repository's releases list (pre-releases count)
with the User-Agent "OpenSkies/<version>": no account, no cookies, nothing about the person or the PC. Offline, a
private repository (404) or any other failure is silent and quick, and the page simply shows nothing. The last
check is kept in the app's cache folder.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import paths

REPO = "metasprojects/open-skies"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
CACHE_FILE = paths.CACHE / "update-check.json"
EVERY = 24 * 3600
TIMEOUT = 4                   # seconds per network step
MAX_BYTES = 2 * 2**20

_VERSION = re.compile(r"[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
_LOCK = threading.Lock()
_THREAD: list[threading.Thread] = []


# ---- versions ------------------------------------------------------------------------------------

def _ident(part: str):
    if part.isdigit():
        return (0, int(part), ())
    # "rc10" after "rc9": digits inside a word count as numbers
    return (1, 0, tuple((0, int(c), "") if c.isdigit() else (1, 0, c.lower()) for c in re.findall(r"\d+|\D+", part)))


def version_key(text) -> tuple | None:
    """A sortable key in semantic-version order (0.1.0-beta.3 < 0.1.0-beta.10 < 0.1.0 < 0.1.1), or None."""
    m = _VERSION.fullmatch(str(text or "").strip())
    if not m:
        return None
    core = tuple(int(x or 0) for x in m.group(1, 2, 3))
    pre = m.group(4)
    if not pre:
        return core + (1, ())                 # a release ranks above all of its pre-releases
    return core + (0, tuple(_ident(p) for p in pre.split(".")))


def is_newer(candidate, than) -> bool:
    a, b = version_key(candidate), version_key(than)
    return bool(a and b and a > b)


def newest_release(releases) -> dict | None:
    """The highest-versioned published release (drafts skipped, pre-releases kept) as {version, tag, url, name}."""
    best = None
    for r in releases if isinstance(releases, list) else []:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        tag = str(r.get("tag_name") or "").strip()
        key = version_key(tag)
        if key and (best is None or key > best[0]):
            best = (key, r, tag)
    if not best:
        return None
    _, r, tag = best
    url = r.get("html_url")
    if not (isinstance(url, str) and url.startswith(f"https://github.com/{REPO}/")):
        url = f"{RELEASES_PAGE}/tag/{urllib.parse.quote(tag)}"
    return {"version": tag[1:] if tag[:1] in "vV" else tag, "tag": tag, "url": url,
            "name": str(r.get("name") or tag)[:80], "prerelease": bool(r.get("prerelease"))}


# ---- the check -----------------------------------------------------------------------------------

def _fetch(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": f"OpenSkies/{paths.APP_VERSION}",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:   # noqa: S310 - fixed https URL
            return r.status, r.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, b""


def _read(cache_file: Path) -> dict:
    try:
        d = json.loads(cache_file.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(cache_file: Path, data: dict) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(cache_file)
    except OSError:
        pass


def check(url: str = RELEASES_API, cache_file: Path = CACHE_FILE, now: float | None = None, fetch=_fetch) -> dict:
    """Ask GitHub unless the last check was under a day ago. Never raises; returns the cache record."""
    now = time.time() if now is None else now
    with _LOCK:
        old = _read(cache_file)
    last = old.get("checkedAt")
    if isinstance(last, (int, float)) and 0 <= now - last < EVERY:
        return old
    rec = {"checkedAt": now, "latest": old.get("latest")}
    t0 = time.time()
    try:
        code, body = fetch(url)
        if code == 200:
            rec["latest"] = newest_release(json.loads(body.decode("utf-8")))
            rec["result"] = "ok"
        else:
            rec["result"] = "not found (private or no releases)" if code == 404 else f"HTTP {code}"
    except Exception as e:  # noqa: BLE001 - offline, DNS, TLS, timeout, bad JSON: all silent
        rec["result"] = f"failed ({e.__class__.__name__})"
    rec["seconds"] = round(time.time() - t0, 2)
    with _LOCK:
        cur = _read(cache_file)
        if cur.get("dismissed"):
            rec["dismissed"] = cur["dismissed"]
        _write(cache_file, rec)
    return rec


def start_background_check() -> None:
    """Start the day's check once per run of the app (later calls do nothing)."""
    with _LOCK:
        if _THREAD:
            return
        t = threading.Thread(target=check, daemon=True, name="update-check")
        _THREAD.append(t)
    t.start()


def status(cache_file: Path = CACHE_FILE) -> dict:
    """For the page: {current, newer: {version, url, name} or None, checking}."""
    with _LOCK:
        c = _read(cache_file)
    latest = c.get("latest") if isinstance(c.get("latest"), dict) else None
    newer = None
    if latest and is_newer(latest.get("version"), paths.APP_VERSION) and latest.get("version") != c.get("dismissed"):
        newer = {k: latest.get(k) for k in ("version", "url", "name", "prerelease")}
    return {"current": paths.APP_VERSION, "newer": newer, "checking": bool(_THREAD and _THREAD[0].is_alive()),
            "checkedAt": c.get("checkedAt"), "result": c.get("result")}


def dismiss(version: str, cache_file: Path = CACHE_FILE) -> None:
    """Hide the notice for this version (a later one shows again)."""
    with _LOCK:
        c = _read(cache_file)
        c["dismissed"] = str(version)[:40]
        _write(cache_file, c)
