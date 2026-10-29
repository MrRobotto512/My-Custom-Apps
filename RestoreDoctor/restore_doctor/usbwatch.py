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
        self.phone_count = 0     # phones on the bus right now; more than one confuses the engine

    # -- public
    def start(self) -> None:
        self._stop.clear()
        target = self._run_windows if IS_WINDOWS else self._run_sysfs
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
                self._report(phone_modes(ids))
        self.available = False

    def _run_sysfs(self) -> None:
        root = Path("/sys/bus/usb/devices")
        if not root.exists():
            self.error = "No USB device list on this system"
            return
        self.available = True
        while not self._stop.is_set():
            modes = []
            for dev in root.iterdir():
                try:
                    if (dev / "idVendor").read_text().strip().lower() != "05ac":
                        continue
                    mode = pid_mode(int((dev / "idProduct").read_text().strip(), 16))
                except (OSError, ValueError):
                    continue
                if mode:
                    modes.append(mode)
            self._report(modes)
            self._stop.wait(0.7)
