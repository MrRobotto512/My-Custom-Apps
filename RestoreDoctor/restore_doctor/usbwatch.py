"""Watches which USB mode the phone is in, independently of the engine.

Why: when a restore dies, "the phone crashed" and "the PC lost the phone"
look the same in the engine's log. Windows still sees the phone on the bus in
the second case, so polling Windows' device list separates them.

Windows: a hidden PowerShell loop lists present Apple USB devices
(Win32_PnPEntity, VID 05AC) and prints a line whenever the set changes. No
admin rights or extra drivers needed.
Linux: reads /sys/bus/usb/devices (used for development).
"""
from __future__ import annotations

import base64
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .paths import IS_WINDOWS

# Apple USB product IDs by mode (libirecovery / restorekit tables)
DFU_PIDS = {0x1227: "DFU", 0x1222: "WTF", 0xF014: "Port DFU", 0x1881: "KIS"}
RECOVERY_PIDS = {0x1280, 0x1281, 0x1282, 0x1283}
PHONE_PID_RANGE = range(0x1290, 0x12B0)   # normal mode, and restore mode while restoring


def pid_mode(pid: int) -> str | None:
    if pid in DFU_PIDS:
        return DFU_PIDS[pid]
    if pid in RECOVERY_PIDS:
        return "Recovery"
    if pid in PHONE_PID_RANGE:
        return "Normal"       # the engine's own log tells normal from restore
    return None


_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$last = '<none>'
while ($true) {
  $ids = Get-CimInstance -ClassName Win32_PnPEntity -Filter "PNPDeviceID LIKE 'USB\\VID_05AC%'" |
         ForEach-Object { $_.PNPDeviceID } | Sort-Object
  $s = ($ids -join ';')
  if ($s -ne $last) { [Console]::Out.WriteLine('DEV ' + $s); [Console]::Out.Flush(); $last = $s }
  Start-Sleep -Milliseconds 700
}
"""

_PID_RE = re.compile(r"VID_05AC&PID_([0-9A-Fa-f]{4})(?!&MI_)", re.I)


def phone_modes(ids: list[str]) -> list[str]:
    """One mode per phone on the bus (duplicates kept, so two phones in DFU count twice)."""
    out = []
    for dev in ids:
        if "&MI_" in dev.upper():
            continue  # interfaces of a composite device; the parent carries the mode
        m = _PID_RE.search(dev)
        if m:
            mode = pid_mode(int(m.group(1), 16))
            if mode:
                out.append(mode)
    return out


def modes_from_ids(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(phone_modes(ids)))


# ------------------------------------------------------------ which phone
# A phone's USB serial string says WHICH phone it is, without talking to it:
#   DFU / Recovery   "CPID:8120 CPRV:11 ... BDID:0E ECID:001C15D43C20802E ... SRNM:[F2LXK1...]"
#   normal / restore the UDID ("00008120-001C15D43C20802E" on A12 and later:
#                    chip id, then the ECID; 40 hex digits on older phones)
# Windows puts that string at the end of the device's PnP id (blanks become
# underscores), Linux in the "serial" file, macOS in ioreg. Reading it is
# passive, so it is safe while another phone is mid-restore.
_F_ECID = re.compile(r"ECID:([0-9A-Fa-f]{6,16})")
_F_CPID = re.compile(r"CPID:([0-9A-Fa-f]{2,8})")
_F_BDID = re.compile(r"BDID:([0-9A-Fa-f]{1,4})")
_F_SRNM = re.compile(r"SRNM:\[([^\]]*)\]")
_UDID_NEW = re.compile(r"^([0-9A-Fa-f]{8})-?([0-9A-Fa-f]{16})$")
_UDID_OLD = re.compile(r"^[0-9A-Fa-f]{40}$")


def norm_udid(u: str | None) -> str | None:
    """One spelling per UDID, so the PnP id and idevice_id's output compare equal."""
    u = (u or "").strip()
    m = _UDID_NEW.match(u)
    if m:
        return (m.group(1) + "-" + m.group(2)).upper()
    if _UDID_OLD.match(u):
        return u.lower()
    return None


def ecid_from_udid(u: str | None) -> int | None:
    m = _UDID_NEW.match((u or "").strip())
    return int(m.group(2), 16) if m else None


def usb_phone(pid: int, serial: str | None) -> dict | None:
    """What one Apple USB device's product id and serial string say about it."""
    mode = pid_mode(pid)
    if not mode:
        return None
    serial = serial or ""
    d = {"mode": mode, "ecid": None, "cpid": None, "bdid": None, "serial": None, "udid": None,
         "raw": serial[:160]}
    if mode == "Normal":
        d["udid"] = norm_udid(serial)
        d["ecid"] = ecid_from_udid(serial)
        return d
    for key, rx in (("ecid", _F_ECID), ("cpid", _F_CPID), ("bdid", _F_BDID)):
        m = rx.search(serial)
        if m:
            d[key] = int(m.group(1), 16)
    m = _F_SRNM.search(serial)
    if m and m.group(1) not in ("", "N/A"):
        d["serial"] = m.group(1)
    return d


def phones_from_ids(ids: list[str]) -> list[dict]:
    """Windows PnP ids -> one usb_phone() dict per phone on the bus."""
    out = []
    for dev in ids:
        if "&MI_" in dev.upper():
            continue
        m = _PID_RE.search(dev)
        if not m:
            continue
        ph = usb_phone(int(m.group(1), 16), dev.rsplit("\\", 1)[-1] if "\\" in dev else "")
        if ph:
            out.append(ph)
    return out


_IOREG_DEV = re.compile(r"\+-o ")
_IOREG_VID = re.compile(r'"idVendor" = (\d+)')
_IOREG_PID = re.compile(r'"idProduct" = (\d+)')
_IOREG_SER = re.compile(r'"(?:USB Serial Number|kUSBSerialNumberString)" = "([^"]*)"')


def phones_from_ioreg(text: str) -> list[dict]:
    """macOS: `ioreg -p IOUSB -l -w 0` -> one usb_phone() dict per phone."""
    out = []
    for block in _IOREG_DEV.split(text)[1:]:
        v, p = _IOREG_VID.search(block), _IOREG_PID.search(block)
        if not v or not p or int(v.group(1)) != 0x05AC:
            continue
        s = _IOREG_SER.search(block)
        ph = usb_phone(int(p.group(1)), s.group(1) if s else "")
        if ph:
            out.append(ph)
    return out


class PhoneUsb:
    """One phone's slice of a UsbWatch: the same surface a Session reads
    (available / timeline / current / since), but only this phone's modes, so
    two phones restoring side by side each keep their own USB evidence.

    ``ecid`` is a callable: a phone that started in DFU may only reveal its
    ECID once the engine has talked to it."""

    def __init__(self, watch, ecid):
        self.watch, self._ecid = watch, ecid
        self.t0 = 0.0                 # set when an attempt starts; see `available`

    def _alone(self) -> bool:
        return self._ecid() is None and getattr(self.watch, "phone_count", 0) <= 1

    @property
    def timeline(self):
        if self._alone():
            return self.watch.timeline
        return self.watch.timeline_for(self._ecid())

    @property
    def available(self) -> bool:
        """False when this phone's modes cannot be told from another phone's
        (then the verdict simply goes without USB evidence, never with the
        wrong phone's)."""
        if not getattr(self.watch, "available", False):
            return False
        if self._alone():
            return True
        if self._ecid() is None:
            return False
        return not any(m == "?" for t, m in self.timeline if t >= self.t0)

    @property
    def current(self):
        tl = self.timeline
        return tl[-1][1] if tl else None

    def since(self, t0: float):
        tl = self.timeline
        out = [(t - t0, m) for t, m in tl if t >= t0]
        before = [m for t, m in tl if t < t0]
        if before and (not out or out[0][0] > 0):
            out.insert(0, (0.0, before[-1]))
        return out


class UsbWatch:
    """Calls ``on_change(t_monotonic, mode)`` whenever the phone's USB mode
    changes. ``mode`` is DFU, Recovery, Normal (normal or restore), WTF,
    Port DFU, or Gone. ``timeline`` keeps every change."""

    def __init__(self, on_change: Callable[[float, str], None] | None = None):
        self.on_change = on_change
        self.timeline: list[tuple[float, str]] = []
        self.available = False
        self.error: str | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._last: str | None = None
        self.phone_count = 0     # phones on the bus right now
        # which phones (see usb_phone); can_list says the list can be trusted
        self.phones: list[dict] = []
        self.can_list = False
        self._tracked: dict[int, list[tuple[float, str]]] = {}   # ecid -> [(t, mode)]
        self._udid_ecid: dict[str, int] = {}                     # learned: UDID -> ECID
        self._plock = threading.Lock()

    # -- public
    def start(self) -> None:
        self._stop.clear()
        import sys
        target = (self._run_windows if IS_WINDOWS
                  else self._run_ioreg if sys.platform == "darwin" else self._run_sysfs)
        self._thread = threading.Thread(target=target, name="usbwatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    @property
    def current(self) -> str | None:
        return self._last

    def since(self, t0: float) -> list[tuple[float, str]]:
        """Timeline from t0 on, with times relative to t0."""
        out = [(t - t0, m) for t, m in self.timeline if t >= t0]
        before = [m for t, m in self.timeline if t < t0]
        if before and (not out or out[0][0] > 0):
            out.insert(0, (0.0, before[-1]))
        return out

    # -- per-phone view
    def track(self, ecid: int | None) -> None:
        """Start keeping a mode timeline for this phone."""
        if ecid is None:
            return
        with self._plock:
            if ecid not in self._tracked:
                self._tracked[ecid] = []
        self._retrack()

    def untrack(self, ecid: int | None) -> None:
        with self._plock:
            self._tracked.pop(ecid, None)

    def know(self, udid: str | None, ecid: int | None) -> None:
        """An older phone's UDID does not contain its ECID; tell the watch."""
        u = norm_udid(udid)
        if u and ecid is not None:
            with self._plock:
                self._udid_ecid[u] = ecid

    def ecid_of(self, ph: dict) -> int | None:
        return ph.get("ecid") if ph.get("ecid") is not None else self._udid_ecid.get(ph.get("udid") or "")

    def timeline_for(self, ecid: int | None) -> list[tuple[float, str]]:
        return list(self._tracked.get(ecid, ())) if ecid is not None else []

    def mode_of(self, ecid: int | None) -> str | None:
        tl = self._tracked.get(ecid) if ecid is not None else None
        return tl[-1][1] if tl else None

    def _retrack(self) -> None:
        """Work out each tracked phone's mode from the current list."""
        t = time.monotonic()
        with self._plock:
            seen: dict[int, str] = {}
            loose = []                       # phones whose ECID the list does not give
            for ph in self.phones:
                e = self.ecid_of(ph)
                if e is not None:
                    seen[e] = ph["mode"]
                elif ph["mode"] == "Normal":
                    loose.append(ph)
            missing = [e for e in self._tracked if e not in seen]
            if len(missing) == 1 and len(loose) == 1:
                # exactly one phone unaccounted for and one unnamed phone: same phone
                seen[missing[0]] = loose[0]["mode"]
                if loose[0].get("udid"):
                    self._udid_ecid[loose[0]["udid"]] = missing[0]
                missing = []
            for e, tl in self._tracked.items():
                mode = seen.get(e) or ("?" if (e in missing and loose) else "Gone")
                if not tl or tl[-1][1] != mode:
                    tl.append((t, mode))

    def _report_phones(self, phones: list[dict]) -> None:
        self.phones = phones
        self.can_list = True
        self._retrack()
        self._report([p["mode"] for p in phones])

    # -- internals
    def _report(self, modes: list[str]) -> None:
        mode = modes[0] if modes else "Gone"
        count_changed = len(modes) != self.phone_count
        self.phone_count = len(modes)
        if mode != self._last or count_changed:
            t = time.monotonic()
            if mode != self._last:
                self.timeline.append((t, mode))
            self._last = mode
            if self.on_change:
                try:
                    self.on_change(t, mode)
                except Exception:
                    pass

    def _run_windows(self) -> None:
        encoded = base64.b64encode(_PS_SCRIPT.encode("utf-16-le")).decode("ascii")
        try:
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", encoded],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self.error = f"PowerShell could not start: {exc}"
            return
        self.available = True
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            if line.startswith("DEV "):
                ids = [x for x in line[4:].strip().split(";") if x]
                self._report_phones(phones_from_ids(ids))
        self.available = False
        self.can_list = False

    def _run_ioreg(self) -> None:
        """macOS: lists the phones (so several can be told apart). The mode
        TIMELINE stays off here, as it always was on a Mac - `available` is
        what feeds USB evidence into a verdict, and this path is new."""
        fails = 0
        while not self._stop.is_set():
            try:
                r = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w", "0"], capture_output=True,
                                   text=True, errors="replace", timeout=8)
                if r.returncode != 0:
                    raise OSError("ioreg failed")
                self._report_phones(phones_from_ioreg(r.stdout))
                fails = 0
            except Exception as exc:  # noqa: BLE001
                fails += 1
                if fails >= 3:
                    self.can_list = False
                    self.error = "ioreg: %s" % exc
            self._stop.wait(1.0)
        self.can_list = False

    def _run_sysfs(self) -> None:
        root = Path("/sys/bus/usb/devices")
        if not root.exists():
            self.error = "No USB device list on this system"
            return
        self.available = True
        while not self._stop.is_set():
            phones = []
            for dev in root.iterdir():
                try:
                    if (dev / "idVendor").read_text().strip().lower() != "05ac":
                        continue
                    pid = int((dev / "idProduct").read_text().strip(), 16)
                except (OSError, ValueError):
                    continue
                try:
                    serial = (dev / "serial").read_text().strip()
                except OSError:
                    serial = ""
                ph = usb_phone(pid, serial)
                if ph:
                    phones.append(ph)
            self._report_phones(phones)
            self._stop.wait(0.7)
