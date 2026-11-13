"""Firmware: which builds Apple signs, the local IPSW folder, downloads."""
from __future__ import annotations

import hashlib
import json
import plistlib
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths

API = "https://api.ipsw.me/v4/device/{product}?type=ipsw"
UA = "RestoreDoctor/0.1 (+idevicerestore)"


@dataclass
class Build:
    product: str
    version: str
    build: str
    signed: bool
    url: str | None = None
    sha1: str | None = None
    sha256: str | None = None
    size: int | None = None
    path: Path | None = None     # set when the IPSW is on disk

    @property
    def label(self) -> str:
        where = "on disk" if self.path else ("download" if self.url else "")
        return f"iOS {self.version} ({self.build}){' - ' + where if where else ''}"


def signed_builds(product: str, max_age: float = 3600) -> list[Build]:
    """Builds Apple currently signs for this model (api.ipsw.me, the same
    source the engine uses for its latest-build option). Cached for an hour."""
    cache = paths.data_dir() / f"signed-{product}.json"
    data = None
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        data = _read_cache(cache)
    if data is None:
        req = urllib.request.Request(API.format(product=product), headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            # Offline: an older list beats none, but say how old it is.
            data = _read_cache(cache) if cache.exists() else None
            if data is None:
                raise
            age_h = (time.time() - cache.stat().st_mtime) / 3600
            raise StaleList(_builds_from(product, data), f"Could not reach api.ipsw.me ({exc}); showing the "
                            f"signed list from {age_h:.0f} h ago. Apple may have stopped signing since.") from exc
        cache.write_text(json.dumps(data), encoding="utf-8")
    return _builds_from(product, data)


class StaleList(Exception):
    """signed_builds() fell back to an out-of-date cached list; ``builds`` holds it."""

    def __init__(self, builds: list[Build], note: str):
        super().__init__(note)
        self.builds = builds


def _read_cache(cache: Path) -> dict | None:
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _builds_from(product: str, data: dict) -> list[Build]:
    out = []
    for fw in data.get("firmwares", []):
        if fw.get("signed"):
            out.append(Build(product, fw.get("version", "?"), fw.get("buildid", "?"), True, fw.get("url"),
                             fw.get("sha1sum"), fw.get("sha256sum"), fw.get("filesize")))
    return out


_ipsw_cache: dict[tuple[str, int, int], dict | None] = {}


def read_ipsw(path: Path) -> dict | None:
    """Product types and version from an IPSW's BuildManifest.plist.
    Cached by path, size and mtime: the folder is rescanned on every USB change."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _ipsw_cache:
        _ipsw_cache[key] = _read_ipsw(path)
    return _ipsw_cache[key]


def _read_ipsw(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path) as z:
            with z.open("BuildManifest.plist") as f:
                bm = plistlib.load(f)
    except (OSError, KeyError, zipfile.BadZipFile, plistlib.InvalidFileException, ValueError):
        return None
    return {
        "products": list(bm.get("SupportedProductTypes", [])),
        "version": bm.get("ProductVersion", "?"),
        "build": bm.get("ProductBuildVersion", "?"),
    }


def local_builds(folder: Path, product: str | None = None) -> list[Build]:
    out = []
    if not folder.exists():
        return out
    for p in sorted(folder.glob("*.ipsw")):
        info = read_ipsw(p)
        if not info:
            continue
        if product and product not in info["products"]:
            continue
        out.append(Build(product or ",".join(info["products"]), info["version"], info["build"], False,
                         path=p))
    return out


def merge(signed: list[Build], local: list[Build]) -> list[Build]:
    """Signed builds first (marking those already on disk), then other local files
    (which can only restore if Apple still signs them)."""
    by_build = {b.build: b for b in local}
    out = []
    for b in signed:
        if b.build in by_build:
            b.path = by_build.pop(b.build).path
        out.append(b)
    out.extend(by_build.values())
    return out


def sha1_of(path: Path, progress: Callable[[float], None] | None = None) -> str:
    h = hashlib.sha1()
    total = path.stat().st_size or 1
    done = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done / total)
    return h.hexdigest()


def download(build: Build, folder: Path, progress: Callable[[float, str], None],
             cancelled: Callable[[], bool]) -> Path:
    """Download (with resume) into folder and verify the SHA-1."""
    assert build.url
    folder.mkdir(parents=True, exist_ok=True)
    name = build.url.rsplit("/", 1)[-1]
    final = folder / name
    part = folder / (name + ".part")
    if final.exists() and (not build.size or final.stat().st_size == build.size):
        return final
    have = part.stat().st_size if part.exists() else 0
    if build.size and have > build.size:
        part.unlink()  # longer than the real file: can't be resumed
        have = 0
    # A .part that is already complete (the app closed while checking it) only needs checking;
    # asking the server for the bytes after the end would get HTTP 416.
    if not (build.size and have == build.size):
        _fetch(build, part, have, progress, cancelled)
    if build.sha1:
        progress(1.0, "Checking the download...")
        digest = sha1_of(part)
        if digest.lower() != build.sha1.lower():
            part.unlink(missing_ok=True)
            raise ValueError("The download is damaged (checksum mismatch). Try again.")
    part.replace(final)
    return final


def _fetch(build: Build, part: Path, have: int, progress: Callable[[float, str], None],
           cancelled: Callable[[], bool]) -> None:
    req = urllib.request.Request(build.url, headers={"User-Agent": UA, **({"Range": f"bytes={have}-"} if have else {})})
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:
            return  # nothing left to fetch; the checksum decides whether the file is good
        raise
    with r:
        if have and r.status != 206:
            have = 0  # server ignored the range; start over
        total = (build.size or (have + int(r.headers.get("Content-Length", 0)))) or 1
        with open(part, "ab" if have else "wb") as f:
            done = have
            last = 0.0
            while True:
                if cancelled():
                    raise InterruptedError("Download cancelled")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last > 0.25:
                    progress(done / total, f"{done / 1e9:.2f} of {total / 1e9:.2f} GB")
                    last = now


# ------------------------------------------------------------ housekeeping
# IPSWs are 6-9 GB each and a busy bench collects them fast. Three policies:
#   keep_newest          (default) one file per model family: the newest iOS
#   delete_after_restore the file is removed once a restore with it succeeds
#   keep_all             nothing is ever removed automatically
# Abandoned partial downloads are swept under every policy.
POLICIES = ("keep_newest", "delete_after_restore", "keep_all")
DEFAULT_POLICY = "keep_newest"
STALE_PART_S = 7 * 86400


def _vkey(version: str) -> list[int]:
    import re
    parts = [int(x) for x in re.findall(r"\d+", version or "")][:4]
    return parts + [0] * (4 - len(parts))


def inventory(folder: Path) -> list[dict]:
    """Every firmware file in the folder (and unfinished downloads), newest first."""
    out = []
    if not folder.exists():
        return out
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".ipsw", ".part"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        info = read_ipsw(p) if p.suffix.lower() == ".ipsw" else None
        out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                    "part": p.suffix.lower() == ".part",
                    "version": (info or {}).get("version", ""), "build": (info or {}).get("build", ""),
                    "products": (info or {}).get("products", [])})
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


def delete_file(folder: Path, name: str) -> bool:
    """Delete one firmware file BY NAME inside the folder - never a path."""
    if not name or Path(name).name != name or Path(name).suffix.lower() not in (".ipsw", ".part"):
        return False
    p = folder / name
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False


def cleanup(folder: Path, policy: str, keep: tuple = (), used: str | None = None) -> list[str]:
    """Apply a housekeeping policy; returns the names removed.

    keep: file names that must survive (the one a restore is using).
    used: the file a restore just succeeded with (delete_after_restore removes it)."""
    removed = []
    files = inventory(folder)
    now = time.time()
    for f in files:                                   # abandoned downloads, any policy
        if f["part"] and now - f["mtime"] > STALE_PART_S and f["name"] not in keep:
            if delete_file(folder, f["name"]):
                removed.append(f["name"])
    if policy == "delete_after_restore":
        if used and used not in keep and delete_file(folder, used):
            removed.append(used)
    elif policy == "keep_newest":
        groups: dict[tuple, list[dict]] = {}
        for f in files:
            if not f["part"] and f["products"]:       # unreadable files are left for a human
                groups.setdefault(tuple(sorted(f["products"])), []).append(f)
        for fam in groups.values():
            fam.sort(key=lambda f: (_vkey(f["version"]), f["mtime"]), reverse=True)
            for f in fam[1:]:
                if f["name"] not in keep and delete_file(folder, f["name"]):
                    removed.append(f["name"])
    return removed
