"""
PhotoPull engine: talks to a paired iPhone/iPad over USB (via Apple Mobile Device
Service / usbmuxd) and copies the camera roll out of DCIM using the AFC service.

Everything here is asyncio. The GUI runs it in a worker thread.
"""
from __future__ import annotations

import asyncio
import os
import posixpath
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from pymobiledevice3 import exceptions as pmd_exc
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.usbmux import list_devices

PHOTO_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".dng", ".gif", ".tif", ".tiff", ".webp", ".bmp"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".hevc", ".3gp", ".avi"}
SIDECAR_EXT = {".aae"}

READ_CHUNK = 4 * 1024 * 1024  # AFC max read per request

LAYOUT_DATE = "date"    # <dest>/<device>/2024/2024-07/IMG_0001.HEIC
LAYOUT_DCIM = "dcim"    # <dest>/<device>/100APPLE/IMG_0001.HEIC
LAYOUT_FLAT = "flat"    # <dest>/<device>/IMG_0001.HEIC

HEIC_KEEP = "keep"      # copy HEIC as-is
HEIC_BOTH = "both"      # copy HEIC and also write a JPG next to it
HEIC_JPG = "jpg"        # write JPG only


class UserFacingError(Exception):
    """An error with a message that can be shown to the user as-is."""


@dataclass
class DeviceInfo:
    serial: str
    connection: str
    name: str = "iOS device"
    model: str = ""
    ios: str = ""
    trusted: Optional[bool] = None

    @property
    def label(self) -> str:
        bits = [self.name]
        extra = ", ".join(x for x in (self.model, f"iOS {self.ios}" if self.ios else "") if x)
        if extra:
            bits.append(f"({extra})")
        if self.trusted is False:
            bits.append("- not trusted yet")
        return " ".join(bits)


@dataclass
class MediaItem:
    device_path: str          # e.g. /DCIM/100APPLE/IMG_0001.HEIC
    folder: str               # e.g. 100APPLE
    name: str                 # e.g. IMG_0001.HEIC
    size: int
    mtime: datetime
    birthtime: datetime
    kind: str = "photo"       # photo | video | live | sidecar
    target: Optional[Path] = None   # filled by plan()
    jpg_target: Optional[Path] = None

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def taken(self) -> datetime:
        # birthtime is when the file was created on the device; fall back to mtime if it looks bogus
        bt = self.birthtime
        if bt.year < 2000 or bt > datetime.now():
            return self.mtime
        return min(bt, self.mtime)


@dataclass
class Options:
    dest: Path
    photos: bool = True
    videos: bool = True
    live: bool = True
    sidecars: bool = False
    layout: str = LAYOUT_DATE
    heic: str = HEIC_KEEP
    workers: int = 3


@dataclass
class Stats:
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_copied: int = 0
    errors: list[str] = field(default_factory=list)


def _friendly(exc: BaseException) -> str:
    if isinstance(exc, pmd_exc.ConnectionFailedToUsbmuxdError):
        return ("Can't reach Apple Mobile Device Service. Install the 'Apple Devices' app "
                "(Microsoft Store) or iTunes, then try again.")
    if isinstance(exc, pmd_exc.PasswordRequiredError):
        return "The device is locked. Unlock it (enter the passcode) and try again."
    if isinstance(exc, pmd_exc.UserDeniedPairingError):
        return "'Don't Trust' was tapped on the device. Unplug, replug and tap Trust."
    if isinstance(exc, pmd_exc.PairingDialogResponsePendingError):
        return "Waiting for you to tap 'Trust' on the device (and enter the passcode)."
    if isinstance(exc, (pmd_exc.NoDeviceConnectedError, pmd_exc.DeviceNotFoundError)):
        return "No device found. Check the cable and that the device is unlocked."
    if isinstance(exc, pmd_exc.ConnectionFailedError):
        return "Lost connection to the device. Check the cable and try again."
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- devices

async def find_devices() -> list[DeviceInfo]:
    try:
        muxdevs = await list_devices()
    except Exception as e:  # usbmuxd not running, etc.
        raise UserFacingError(_friendly(e)) from e

    seen: dict[str, DeviceInfo] = {}
    for md in muxdevs:
        # same phone can show up twice (USB + Wi-Fi). Prefer USB.
        if md.serial in seen and seen[md.serial].connection == "USB":
            continue
        info = DeviceInfo(serial=md.serial, connection=md.connection_type)
        try:
            ld = await create_using_usbmux(serial=md.serial, autopair=False)
            try:
                v = ld.all_values
                info.name = v.get("DeviceName") or ld.display_name or "iOS device"
                info.model = ld.display_name or v.get("ProductType") or ""
                info.ios = v.get("ProductVersion") or ""
                info.trusted = bool(getattr(ld, "paired", False))
            finally:
                await ld.close()
        except Exception:
            pass
        seen[md.serial] = info
    return list(seen.values())


async def connect(serial: str, pair_timeout: float = 60.0):
    """Open a trusted lockdown session. Triggers the Trust prompt if needed."""
    try:
        return await create_using_usbmux(serial=serial, autopair=True, pair_timeout=pair_timeout)
    except Exception as e:
        raise UserFacingError(_friendly(e)) from e


# --------------------------------------------------------------------------- scan

def _classify(name: str) -> Optional[str]:
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT:
        return "sidecar"
    return None


async def scan(afc: AfcService, progress: Callable[[str], None], cancel) -> list[MediaItem]:
    try:
        folders = [f for f in await afc.listdir("DCIM") if f and not f.startswith(".")]
    except pmd_exc.AfcFileNotFoundError:
        return []
    folders.sort()

    items: list[MediaItem] = []
    sem = asyncio.Semaphore(16)

    async def stat_one(folder: str, name: str) -> Optional[MediaItem]:
        path = posixpath.join("/DCIM", folder, name)
        async with sem:
            if cancel.is_set():
                return None
            try:
                st = await afc.stat(path)
            except Exception:
                return None
        if st.get("st_ifmt") == "S_IFDIR":
            return None
        return MediaItem(path, folder, name, st["st_size"], st["st_mtime"], st["st_birthtime"],
                         _classify(name) or "photo")

    for i, folder in enumerate(folders, 1):
        if cancel.is_set():
            break
        progress(f"Scanning {folder} ({i}/{len(folders)})...")
        try:
            names = [n for n in await afc.listdir(posixpath.join("/DCIM", folder))
                     if n not in (".", "..", "") and not n.startswith(".") and _classify(n)]
        except Exception:
            continue
        results = await asyncio.gather(*(stat_one(folder, n) for n in names))
        folder_items = [r for r in results if r is not None]

        # Live Photo motion clips: IMG_1234.MOV next to IMG_1234.HEIC/JPG
        photo_stems = {it.stem.upper() for it in folder_items if it.kind == "photo"}
        for it in folder_items:
            if it.kind == "video" and it.ext == ".mov" and it.stem.upper() in photo_stems:
                it.kind = "live"
        items.extend(folder_items)

    items.sort(key=lambda it: (it.folder, it.name))
    return items


# --------------------------------------------------------------------------- plan

def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in name).strip(" .")
    return out or "iOS device"


def select(items: list[MediaItem], opt: Options) -> list[MediaItem]:
    want = {"photo": opt.photos, "video": opt.videos, "live": opt.live, "sidecar": opt.sidecars}
    return [it for it in items if want.get(it.kind, False)]


def plan(items: list[MediaItem], opt: Options, device_name: str) -> list[MediaItem]:
    root = opt.dest / _safe(device_name)

    def subdir(it: MediaItem) -> Path:
        if opt.layout == LAYOUT_DCIM:
            return root / it.folder
        if opt.layout == LAYOUT_FLAT:
            return root
        t = it.taken
        return root / f"{t:%Y}" / f"{t:%Y-%m}"

    def out_name(it: MediaItem, disambiguate: bool) -> str:
        stem, ext = it.stem, os.path.splitext(it.name)[1]
        if disambiguate:
            stem = f"{stem}_{it.folder}"
        if it.ext in (".heic", ".heif") and opt.heic == HEIC_JPG:
            ext = ".jpg"
        return stem + ext

    # Deterministic collision handling so re-runs map to the same files (lets resume work).
    first: dict[str, MediaItem] = {}
    clashes: set[int] = set()
    for it in items:
        key = str(subdir(it) / out_name(it, False)).lower()
        if key in first and first[key].device_path != it.device_path:
            clashes.add(id(it))
            clashes.add(id(first[key]))
        else:
            first.setdefault(key, it)

    for it in items:
        d = subdir(it)
        it.target = d / out_name(it, id(it) in clashes)
        it.jpg_target = None
        if it.ext in (".heic", ".heif") and opt.heic == HEIC_BOTH:
            it.jpg_target = it.target.with_suffix(".jpg")
    return items


def already_done(it: MediaItem, opt: Options) -> bool:
    t = it.target
    if t is None or not t.exists():
        return False
    if it.ext in (".heic", ".heif") and opt.heic == HEIC_JPG:
        return t.stat().st_size > 0  # converted output, size won't match
    if t.stat().st_size != it.size:
        return False
    if it.jpg_target is not None and not it.jpg_target.exists():
        return False
    return True


# --------------------------------------------------------------------------- copy

def _set_times(path: Path, created: datetime, modified: datetime) -> None:
    m = modified.timestamp()
    os.utime(path, (m, m))
    if sys.platform != "win32":
        return
    try:  # also set "Date created" on Windows so Explorer sorts sensibly
        import ctypes
        from ctypes import wintypes

        def ft(dt: datetime) -> wintypes.FILETIME:
            v = int((dt.timestamp() + 11644473600) * 10_000_000)
            return wintypes.FILETIME(v & 0xFFFFFFFF, v >> 32)

        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        h = k32.CreateFileW(str(path), 0x0100, 0x7, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES
        if h and h != wintypes.HANDLE(-1).value:
            c, w = ft(created), ft(modified)
            k32.SetFileTime(h, ctypes.byref(c), None, ctypes.byref(w))
            k32.CloseHandle(h)
    except Exception:
        pass


def heic_to_jpg(src: Path, dst: Path, quality: int = 92) -> None:
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()
    with Image.open(src) as im:
        exif = im.info.get("exif")       # orientation already reset to 1; pixels are upright
        icc = im.info.get("icc_profile")  # keep Display P3 so colors don't shift
        rgb = im.convert("RGB")
        kw = {"quality": quality, "subsampling": 0}
        if exif:
            kw["exif"] = exif
        if icc:
            kw["icc_profile"] = icc
        tmp = dst.with_name(dst.name + ".part")
        rgb.save(tmp, "JPEG", **kw)
        os.replace(tmp, dst)


def heic_supported() -> bool:
    try:
        import pillow_heif  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


async def _pull_file(afc: AfcService, it: MediaItem, dst: Path, cancel, on_bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    handle = await afc.fopen(it.device_path, "r")
    got = 0
    try:
        with open(part, "wb") as f:
            while got < it.size:
                if cancel.is_set():
                    raise asyncio.CancelledError()
                chunk = await afc.fread(handle, min(READ_CHUNK, it.size - got))
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                on_bytes(len(chunk))
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            await afc.fclose(handle)
        except Exception:
            pass
    if got != it.size:
        part.unlink(missing_ok=True)
        raise IOError(f"short read ({got} of {it.size} bytes)")
    os.replace(part, dst)


async def copy_all(afc: AfcService, items: list[MediaItem], opt: Options, cancel,
                   on_progress: Callable[[int, int, int, int, str], None]) -> Stats:
    """on_progress(files_done, files_total, bytes_done, bytes_total, current_name)"""
    stats = Stats()
    todo: list[MediaItem] = []
    for it in items:
        if already_done(it, opt):
            stats.skipped += 1
        else:
            todo.append(it)

    total_files = len(items)
    total_bytes = sum(it.size for it in todo)
    state = {"bytes": 0, "done": stats.skipped}
    on_progress(state["done"], total_files, 0, total_bytes, "")

    def add_bytes(n: int) -> None:
        state["bytes"] += n

    queue: asyncio.Queue[MediaItem] = asyncio.Queue()
    for it in todo:
        queue.put_nowait(it)

    async def worker() -> None:
        while not cancel.is_set():
            try:
                it = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            on_progress(state["done"], total_files, state["bytes"], total_bytes, it.name)
            try:
                convert_only = it.ext in (".heic", ".heif") and opt.heic == HEIC_JPG
                raw_dst = it.target.with_name(it.stem + "_src" + it.ext) if convert_only else it.target
                await _pull_file(afc, it, raw_dst, cancel, add_bytes)
                if convert_only or it.jpg_target is not None:
                    jpg = it.target if convert_only else it.jpg_target
                    try:
                        await asyncio.to_thread(heic_to_jpg, raw_dst, jpg)
                        _set_times(jpg, it.taken, it.mtime)
                    finally:
                        if convert_only:
                            raw_dst.unlink(missing_ok=True)
                if not convert_only:
                    _set_times(raw_dst, it.taken, it.mtime)
                stats.copied += 1
                stats.bytes_copied += it.size
            except asyncio.CancelledError:
                return
            except Exception as e:
                stats.failed += 1
                stats.errors.append(f"{it.device_path}: {_friendly(e)}")
            finally:
                state["done"] += 1
                on_progress(state["done"], total_files, state["bytes"], total_bytes, "")

    await asyncio.gather(*(worker() for _ in range(max(1, opt.workers))))
    return stats


def write_log(dest_root: Path, stats: Stats, device: DeviceInfo) -> Optional[Path]:
    if not stats.errors:
        return None
    dest_root.mkdir(parents=True, exist_ok=True)
    p = dest_root / f"PhotoPull-errors-{datetime.now():%Y%m%d-%H%M%S}.txt"
    p.write_text(f"Device: {device.label} [{device.serial}]\n\n" + "\n".join(stats.errors), encoding="utf-8")
    return p
