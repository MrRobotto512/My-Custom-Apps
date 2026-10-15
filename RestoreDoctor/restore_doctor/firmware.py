"""Firmware: which builds Apple signs, the local IPSW folder, downloads."""
from __future__ import annotations

import hashlib
import json
import plistlib
import time
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
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            data = None
    if data is None:
        req = urllib.request.Request(API.format(product=product), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        cache.write_text(json.dumps(data), encoding="utf-8")
    out = []
    for fw in data.get("firmwares", []):
        if fw.get("signed"):
            out.append(Build(product, fw.get("version", "?"), fw.get("buildid", "?"), True, fw.get("url"),
                             fw.get("sha1sum"), fw.get("sha256sum"), fw.get("filesize")))
    return out


def read_ipsw(path: Path) -> dict | None:
    """Product types and version from an IPSW's BuildManifest.plist."""
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
    req = urllib.request.Request(build.url, headers={"User-Agent": UA, **({"Range": f"bytes={have}-"} if have else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
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
    if build.sha1:
        progress(1.0, "Checking the download...")
        digest = sha1_of(part)
        if digest.lower() != build.sha1.lower():
            part.unlink(missing_ok=True)
            raise ValueError("The download is damaged (checksum mismatch). Try again.")
    part.replace(final)
    return final
