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

print("\nALL OK" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
