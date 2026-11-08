"""Downloads: pinned installers with fingerprints, the shared open-data files, the current Overture release."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import paths

# Pinned, checked by SHA-256 before anything is unpacked.
BEPINEX = {"url": "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.4/BepInEx_win_x64_5.4.23.4.zip",
           "sha256": "f881201b79da03e513bf97cdf39607ffa7f9e0d31a519b1aeeca8eb60f8309e7", "version": "5.4.23.4"}
NOCUSTOMMAPS = {"url": "https://github.com/BatongMaster/NOCustomMaps/releases/download/v1.4.2/NOCustomMaps-1.4.2.zip",
                "sha256": "1258bb9bd9c58ed8a71b5abf05b4002c09f301fd830e34ffb364f9f45233736f", "version": "1.4.2"}
# Unity's installer for 64-bit Windows (the old UnityHubSetup.exe name answers 404 since the arm64 split).
UNITY_HUB_SETUP = "https://public-cdn.cloud.unity3d.com/hub/prod/UnityHubSetup-x64.exe"

OURAIRPORTS = "https://davidmegginson.github.io/ourairports-data"
# World Bank / IMF Global Shipping Traffic Density (CC BY 4.0), dataset 0037580.
SHIPPING = {
    "shipdensity_commercial_.zip":
        ("https://datacatalogfiles.worldbank.org/ddh-published/0037580/DR0045405/shipdensity_commercial_.zip", 480353369),
    "ShipDensity_Passenger.zip":
        ("https://datacatalogfiles.worldbank.org/ddh-published/0037580/DR0045404/ShipDensity_Passenger.zip", 22849854),
}
SHIPPING_MB = round(sum(s for _, s in SHIPPING.values()) / 1e6)

OVERTURE_BUCKET = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
OVERTURE_PINNED = "2026-10-21.0"   # the release the map engine's queries were written against


class DownloadError(RuntimeError):
    pass


def fetch(url: str, dest: Path, expect_size: int | None = None, sha256: str | None = None, progress=None,
          timeout: int = 120) -> Path:
    """Download url to dest (resumable, atomic). progress(done_bytes, total_bytes) is called as it goes."""
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (expect_size is None or dest.stat().st_size == expect_size) and \
            (sha256 is None or file_sha256(dest) == sha256):
        return dest
    part = dest.with_name(dest.name + ".part")
    for attempt in range(4):
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": paths.USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
                if r.status_code == 416:      # already complete
                    break
                if r.status_code not in (200, 206):
                    raise DownloadError(f"{url} answered {r.status_code}")
                if r.status_code == 200:
                    have = 0
                total = have + int(r.headers.get("content-length") or 0)
                with open(part, "ab" if have else "wb") as f:
                    done = have
                    last = 0.0
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if progress and time.time() - last > 0.5:
                            progress(done, total)
                            last = time.time()
            break
        except (requests.RequestException, OSError) as e:
            if attempt == 3:
                raise DownloadError(f"could not download {url}: {e}") from e
            time.sleep(3 * (attempt + 1))
    if expect_size is not None and part.stat().st_size != expect_size:
        raise DownloadError(f"{dest.name}: got {part.stat().st_size:,} bytes, expected {expect_size:,}")
    if sha256 and file_sha256(part) != sha256:
        part.unlink()
        raise DownloadError(f"{dest.name} did not match its fingerprint; nothing was installed")
    part.replace(dest)
    return dest


def authenticode_signer(p: Path) -> str:
    """The subject of a valid Authenticode signature on an .exe, or "" if it is unsigned or the signature is bad."""
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"$s = Get-AuthenticodeSignature -LiteralPath '{p}'; if ($s.Status -eq 'Valid') {{ $s.SignerCertificate.Subject }}"],
            capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ourairports(progress=None) -> Path:
    d = paths.CACHE / "ourairports"
    for name in ("airports.csv", "runways.csv"):
        f = d / name
        # Refresh once a month: airports open, close and get new runways.
        if not f.exists() or time.time() - f.stat().st_mtime > 30 * 86400:
            tmp = d / (name + ".new")
            fetch(f"{OURAIRPORTS}/{name}", tmp, progress=progress)
            tmp.replace(f)
    return d


def shipping(progress=None) -> Path:
    d = paths.CACHE / "shipping"
    total = sum(s for _, s in SHIPPING.values())
    base = 0
    for name, (url, size) in SHIPPING.items():
        fetch(url, d / name, expect_size=size,
              progress=(lambda done, _t, b=base: progress(b + done, total)) if progress else None, timeout=300)
        base += size
    return d


def shipping_ready() -> bool:
    d = paths.CACHE / "shipping"
    return all((d / n).exists() and (d / n).stat().st_size == s for n, (_, s) in SHIPPING.items())


def overture_release() -> str:
    """The Overture release to read. Overture keeps only the last two or three monthly releases online, so the
    pinned one disappears after a few months: then the newest one still published is used."""
    import requests
    try:
        r = requests.get(f"{OVERTURE_BUCKET}/?list-type=2&prefix=release/&delimiter=/", timeout=30,
                         headers={"User-Agent": paths.USER_AGENT})
        r.raise_for_status()
        rel = sorted(re.findall(r"<Prefix>release/([0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+)/</Prefix>", r.text))
    except Exception:  # noqa: BLE001 - offline: try the pinned one and let the stage report
        return OVERTURE_PINNED
    if not rel:
        return OVERTURE_PINNED
    return OVERTURE_PINNED if OVERTURE_PINNED in rel else rel[-1]
