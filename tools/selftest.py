"""Checks of code paths that are hard to trigger in a real build (developer tool, not shipped).

    .venv\\Scripts\\python tools\\selftest.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import airports, build, downloads  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}{(': ' + str(detail)) if detail else ''}")
    if not ok:
        fails.append(name)


# 1. Authenticode: Windows' own notepad is signed by Microsoft; a text file is not signed.
signer = downloads.authenticode_signer(Path(r"C:\Windows\System32\notepad.exe"))
check("signed exe has a signer", "Microsoft" in signer, signer[:60])
with tempfile.TemporaryDirectory(dir=ROOT / "cache") as tmp:
    fake = Path(tmp) / "UnityHubSetup-x64.exe"
    fake.write_bytes(b"MZ not really a program")
    check("unsigned file has no signer", downloads.authenticode_signer(fake) == "")

    # 2. A stage that runs out of memory once is retried and then succeeds.
    tool = Path(tmp) / "flaky.py"
    marker = Path(tmp) / "ran-once"
    tool.write_text(f"import sys, pathlib\nm = pathlib.Path(r'{marker}')\n"
                    "if not m.exists():\n    m.write_text('x'); print('numpy.core._exceptions.MemoryError: Unable to "
                    "allocate 1.2 GiB'); sys.exit(1)\nprint('second try fine')\n", encoding="utf-8")
    cfg = {"mapId": "selftest", "displayName": "Selftest", "center": {"lat": 0, "lon": 0}, "tilesPerSide": 11}
    job = build.Job(cfg)
    build.time.sleep = lambda s: None           # no waiting in the test
    job.mem_check = lambda stage, need: None
    tail = job.run_tool("heights", [tool])
    check("memory failure retried once", tail[-1] == "second try fine", tail[-1:])

    # ...and a stage that keeps failing for memory stops with the plain message.
    always = Path(tmp) / "always.py"
    always.write_text("print('MemoryError: Unable to allocate'); raise SystemExit(1)\n", encoding="utf-8")
    try:
        job.run_tool("roads", [always])
        check("repeated memory failure stops", False)
    except build.StageError as e:
        check("repeated memory failure stops", e.title.startswith("Ran out of memory during 'Road network'"), e.title)
    job.log_path.unlink(missing_ok=True)

# 3. Plain-English errors.
cases = {
    "requests.exceptions.ConnectionError: Max retries exceeded": "Could not download",
    "OSError: [Errno 28] No space left on device": "The disk filled up",
    "PermissionError: [WinError 32] Access is denied": "A file was locked",
    "ValueError: something odd": "'Road network' failed",
    "_duckdb.OutOfMemoryException: Out of Memory Error: Allocation failure": "Ran out of memory",
}
for text, want in cases.items():
    got = build._explain("roads", 1, [text]).title
    check(f"error '{text[:30]}...'", got.startswith(want), got)

# 4. Names the loader and the file system accept.
check("map id rule", build.sanitise_id("São Tomé & Príncipe!") == "sao-tome-principe", build.sanitise_id("São Tomé & Príncipe!"))
check("non-Latin name falls back", build.display_name("台北") == "Open Skies map")
check("asset-safe airport name", airports.ascii_name("(*)Base: North/South*") == "Base North South",
      airports.ascii_name("(*)Base: North/South*"))

# 5. Two airports whose levelled ground would overlap are refused.
a = {"ident": "A", "name": "Alpha", "short": "Alpha", "x": 0, "z": 0, "runwayFt": 9000, "ok": True, "selected": True}
b = {"ident": "B", "name": "Bravo", "short": "Bravo", "x": 1500, "z": 0, "runwayFt": 6000, "ok": True, "selected": True}
probs = airports.check_selection([a, b])
check("overlapping airports refused", any("overlap" in p for p in probs), probs)
check("no airports refused", any("at least one" in p for p in airports.check_selection([])))

# 6. The README written next to every map: credits, the ODbL offer and the EULA notice.
import json  # noqa: E402
from app import paths  # noqa: E402
with tempfile.TemporaryDirectory(dir=ROOT / "cache") as tmp:
    b, o = Path(tmp) / "build", Path(tmp) / "out"
    b.mkdir()
    o.mkdir()
    (b / "manifest.json").write_text(json.dumps({"mapSizeX": 56320, "credits": "CREDITS-LINE", "notice": "NOTICE-LINE"}))
    (b / "airbases.json").write_text(json.dumps([{"displayName": "Alpha", "ident": "XXXX", "faction": "Boscali"}]))
    cfg = {"displayName": "Test", "mapId": "test", "center": {"lat": 1.0, "lon": 2.0}}
    build.write_readme(cfg, b, o, "cm.test.00000000", "Test Free Flight", ["a note"])
    text = (o / "README.txt").read_text(encoding="utf-8")
    check("map README has credits, notice and ODbL offer",
          all(s in text for s in ("CREDITS-LINE", "NOTICE-LINE", paths.SHOCKFRONT_NOTICE, "Open Database License",
                                  "cm.test.00000000", "a note")))

# 7. Area limits.
check("too big refused", bool(build.area_problems(35, 25, 60)))
check("polar refused", bool(build.area_problems(85, 25, 51)))
check("antimeridian refused", bool(build.area_problems(0, 179.5, 51)))
check("Crete accepted", build.area_problems(35.25, 24.9, 51) == [])

# 8. Update notice: version order, picking the newest release, and a check that stays quiet when it can't ask.
import os  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import zipfile  # noqa: E402
from app import report, updates  # noqa: E402
chain = ["0.1.0-beta.3", "0.1.0-beta.9", "0.1.0-beta.10", "0.1.0-rc2", "0.1.0", "0.1.1", "0.2.0", "1.0.0"]
keys = [updates.version_key(v) for v in chain]
check("version order beta.3 < beta.10 < 0.1.0 < 0.1.1", all(k is not None for k in keys) and
      all(a < b for a, b in zip(keys, keys[1:])), [v for v, k in zip(chain, keys) if k is None])
check("v-prefixed tag is newer", updates.is_newer("v0.1.0-beta.4", "0.1.0-beta.3"))
check("same or older is not newer", not updates.is_newer("0.1.0-beta.3", "0.1.0-beta.3")
      and not updates.is_newer("0.1.0-beta.10", "0.1.0"))
check("junk tag ignored", updates.version_key("nightly") is None and not updates.is_newer("nightly", "0.1.0"))
rels = [{"tag_name": "v9.9.9", "draft": True},
        {"tag_name": "v0.1.0-beta.9", "prerelease": True, "html_url": "https://github.com/metasprojects/open-skies/releases/tag/v0.1.0-beta.9"},
        {"tag_name": "v0.1.0-beta.10", "prerelease": True, "html_url": "https://example.com/elsewhere"},
        {"tag_name": "latest-build"}]
new = updates.newest_release(rels)
check("newest release: drafts skipped, pre-release kept, link kept on the repo",
      bool(new) and new["version"] == "0.1.0-beta.10"
      and new["url"] == "https://github.com/metasprojects/open-skies/releases/tag/v0.1.0-beta.10", new)
with tempfile.TemporaryDirectory(dir=ROOT / "cache") as tmp:
    cache = Path(tmp) / "update-check.json"
    calls = []

    def offline(url):
        calls.append(url)
        raise urllib.error.URLError("no network")
    t0 = time.time()
    rec = updates.check(cache_file=cache, now=1000.0, fetch=offline)
    check("offline check is silent and quick", rec.get("latest") is None and time.time() - t0 < 1, rec)
    updates.check(cache_file=cache, now=1000.0 + 3600, fetch=offline)
    check("at most one check a day", len(calls) == 1, len(calls))
    rec = updates.check(cache_file=cache, now=1000.0 + 90000, fetch=lambda url: (404, b""))
    check("private repo (404) is silent", rec.get("latest") is None and "404" not in str(updates.status(cache)["newer"]),
          rec.get("result"))
    rec = updates.check(cache_file=cache, now=1000.0 + 200000, fetch=lambda url: (200, json.dumps(rels).encode()))
    st = updates.status(cache)
    check("newer release shows", bool(st["newer"]) and st["newer"]["version"] == "0.1.0-beta.10", st)
    updates.dismiss("0.1.0-beta.10", cache)
    check("dismissed release hides", updates.status(cache)["newer"] is None)

# 9. Problem report: the user name, home folder and computer name come out everywhere.
home = r"Q:\Profiles\Pat Tester"
scrub = report.make_scrubber([home, r"Q:\Profiles\PATTES~1"], ["Pat Tester", "PATTES~1"], ["PAT-PC"])
cases = {
    home + r"\AppData\Local\Temp\a.log": r"%USERPROFILE%\AppData\Local\Temp\a.log",
    "q:/profiles/pat tester/Documents/x": "%USERPROFILE%/Documents/x",
    r"Q:\Profiles\PATTES~1\AppData": r"%USERPROFILE%\AppData",
    "file:///Q:/Profiles/Pat%20Tester/x": "file:///%USERPROFILE%/x",
    "signed in as Pat Tester.": "signed in as <user>.",
    "host PAT-PC is up": "host <pc> is up",
    r"Q:\Profiles\Pat Testers\x": r"Q:\Profiles\Pat Testers\x",          # someone else's folder stays
}
for raw, want in cases.items():
    check(f"scrub '{raw[:34]}'", scrub(raw) == want, scrub(raw))
as_json = scrub(json.dumps({"p": home + r"\x", "q": repr(Path(home) / "y")}))
check("scrub JSON-escaped and repr paths", json.loads(as_json)["p"] == r"%USERPROFILE%\x" and "pat tester" not in as_json.lower(), as_json)

with tempfile.TemporaryDirectory(dir=ROOT / "cache") as tmp:
    logs = Path(tmp) / "logs"
    logs.mkdir()
    (logs / "old.log").write_text("old line\n" * 12000, encoding="utf-8")                  # ~105 KB, oldest
    (logs / "big.log").write_text("big line\n" * 34000, encoding="utf-8")                  # ~300 KB
    (logs / "new.log").write_text(f"Traceback: File \"{home}\\app\\x.py\"\nuser Pat Tester on PAT-PC\n", encoding="utf-8")
    (logs / "notes.txt").write_text("not a log", encoding="utf-8")
    for i, n in enumerate(("old.log", "big.log", "new.log")):
        os.utime(logs / n, (1_700_000_000 + i * 60,) * 2)
    real_cfg = paths.load_config
    paths.load_config = lambda: {"game_dir": "SECRET-SETTING-42", "unity_editor": "SECRET-SETTING-43"}
    try:
        r = report.make_report(logs_dir=logs, out_dir=Path(tmp), cap=200 * 1024, scrub=scrub)
    finally:
        paths.load_config = real_cfg
    with zipfile.ZipFile(r["file"]) as z:
        body = {n: z.read(n).decode("utf-8") for n in z.namelist()}
    summary = json.loads(body["report.json"])
    check("report has its parts", {"README.txt", "report.json", "setup-status.json", "build-status.json",
                                   "logs/new.log", "logs/big.log"} <= set(body), sorted(body))
    check("report logs newest first, capped", [x["name"] for x in summary["logs"]["included"]] == ["new.log", "big.log"]
          and [x["name"] for x in summary["logs"]["leftOut"]] == ["old.log"] and "logs/notes.txt" not in body
          and summary["logs"]["included"][1]["cut"], summary["logs"])
    check("report scrubbed", "%USERPROFILE%\\app\\x.py" in body["logs/new.log"] and
          not any(s in t.lower() for t in body.values() for s in ("pat tester", "pattes~1", "pat-pc")))
    check("report has config keys, not values", summary["configKeysSet"] == ["game_dir", "unity_editor"] and
          not any("SECRET-SETTING" in t for t in body.values()))
    check("report has version, Windows, memory, disk", summary["version"] == paths.APP_VERSION and
          summary["windows"].get("platform") and "freeMB" in summary["memory"] and summary["disk"][0]["freeGB"] >= 0,
          {k: summary[k] for k in ("version", "memory", "disk")})

# 10. Escalation and Strike missions on a real finished build (Gibraltar: one Boscali airbase, Primeva at sea).
# Skipped when the developer test build isn't on this machine (data\ is not shipped).
import subprocess  # noqa: E402
tb = paths.BUILD / "resume-test"
if (tb / "heights.npy").exists() and (paths.MAPS / "resume-test" / "map.json").exists():
    with tempfile.TemporaryDirectory(dir=ROOT / "cache") as tmp:
        env = dict(os.environ, PYTHONUTF8="1", OPENBLAS_NUM_THREADS="1")
        r = subprocess.run([sys.executable, "-u", str(ROOT / "app" / "scenario_missions.py"),
                            str(paths.MAPS / "resume-test" / "map.json"), str(tb), "cm.resume-test.00000000", tmp,
                            "Gibraltar"], cwd=ROOT / "pipeline", env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        last = (r.stdout.strip().splitlines() or ["{}"])[-1]
        res = json.loads(last) if last.startswith("{") else {}
        check("Escalation and Strike missions made", r.returncode == 0 and
              res.get("made") == ["Gibraltar - Escalation", "Gibraltar - Strike on Gibraltar"], last or r.stderr[-300:])
        lint = [subprocess.run([sys.executable, str(ROOT / "pipeline" / "mission_lint.py"),
                                str(Path(tmp, t, t + ".json")), str(tb)], cwd=ROOT / "pipeline", env=env,
                               capture_output=True, text=True).stdout.strip().splitlines()[-1:]
                for t in res.get("made", [])]
        check("extra missions pass the strict lint", bool(lint) and all(x == ["0 errors, 0 warnings"] for x in lint), lint)
        o = Path(tmp)
        (o / "manifest.json").write_text(json.dumps({"mapSizeX": 1, "credits": "c", "notice": "n"}))
        (o / "airbases.json").write_text(json.dumps([{"displayName": "A", "ident": "X", "faction": "Boscali"}]))
        (o / "missions").mkdir()
        for t in res.get("made", []):
            (o / "missions" / t).mkdir()
        build.write_readme({"displayName": "G", "mapId": "g", "center": {"lat": 0, "lon": 0}}, o, o, "cm.g.0", "x", [])
        text = (o / "README.txt").read_text(encoding="utf-8")
        check("map README lists every mission", all(f"missions\\{t}\\" in text for t in res.get("made", ["?"])))
else:
    print("skip extra missions (no data\\build\\resume-test on this machine)")

print("\nALL OK" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
