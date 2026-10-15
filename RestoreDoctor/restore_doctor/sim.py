"""Simulation mode (RD_SIM=1 or the Simulation menu): replays scenario logs in
place of the engine, with a matching USB watch and device probe, so the screens
and the rules can be exercised without a phone."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .engine import DeviceInfo
from .models import find_by_product
from .paths import resource_dir

TS = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3}) ")


def scenarios() -> list[dict]:
    path = resource_dir() / "scenarios" / "scenarios.json"
    return json.loads(path.read_text(encoding="utf-8"))["scenarios"]


def scenario(sid: str) -> dict:
    for s in scenarios():
        if s["id"] == sid:
            return s
    raise KeyError(sid)


def _timed_lines(sc: dict) -> list[tuple[float, str, str]]:
    """[(scenario_time, stream, line)] - untimed lines inherit the previous time."""
    out, t, t0 = [], 0.0, None
    for line in (resource_dir() / "scenarios" / sc["log"]).read_text(encoding="utf-8").splitlines():
        m = TS.match(line)
        if m:
            ms = ((int(m.group(1)) * 60 + int(m.group(2))) * 60 + int(m.group(3))) * 1000 + int(m.group(4))
            t0 = ms if t0 is None else t0
            t = (ms - t0) / 1000.0
        stream = "out" if line.startswith("progress:") else "err"
        out.append((t, stream, line + "\n"))
    return out


class SimUsbWatch:
    """Same surface as usbwatch.UsbWatch, driven by a SimJob."""

    def __init__(self, on_change=None):
        self.on_change = on_change
        self.timeline: list[tuple[float, str]] = []
        self.available = True
        self.error = None
        self._last = None

    def start(self):
        pass

    def stop(self):
        pass

    @property
    def current(self):
        return self._last

    def since(self, t0: float):
        out = [(t - t0, m) for t, m in self.timeline if t >= t0]
        before = [m for t, m in self.timeline if t < t0]
        if before and (not out or out[0][0] > 0):
            out.insert(0, (0.0, before[-1]))
        return out

    def push(self, mode: str):
        if mode != self._last:
            self._last = mode
            t = time.monotonic()
            self.timeline.append((t, mode))
            if self.on_change:
                self.on_change(t, mode)


class SimDevice:
    """Pretends a phone is attached, per the chosen scenario."""

    def __init__(self, sid: str):
        self.sid = sid

    def probe(self) -> DeviceInfo:
        sc = scenario(self.sid)
        if not sc.get("product"):
            return DeviceInfo()
        model = find_by_product(sc["product"])
        mode = sc["usb"][0][1] if sc.get("usb") else "Recovery"
        return DeviceInfo(mode=mode, ecid=0x0019ED4B21A3C94E, product=sc["product"], board=sc.get("board"),
                          name=model.name if model else sc.get("device"),
                          cpid=model.cpid if model else None, bdid=model.bdid if model else None)


class SimJob:
    """Stands in for engine.RestoreJob."""

    def __init__(self, sid: str, workdir: Path, on_line: Callable[[str, str], None],
                 on_exit: Callable[[int, bool], None], usb: SimUsbWatch | None = None, speed: float = 10.0):
        self.sc = scenario(sid)
        self.workdir = Path(workdir)
        self.on_line, self.on_exit = on_line, on_exit
        self.usb = usb
        self.speed = speed
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.raw_log = self.workdir / "engine.log"
        self.started_at: float | None = None

    def start(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        lines = _timed_lines(self.sc)
        usb = [(t, m) for t, m in self.sc.get("usb", [])]
        events = [(t, 0, "usb", m) for t, m in usb] + [(t, 1, s, ln) for t, s, ln in lines]
        events.sort(key=lambda e: (e[0], e[1]))
        last_t = 0.0
        with open(self.raw_log, "w", encoding="utf-8") as log:
            for t, _, kind, payload in events:
                gap = max(0.0, t - last_t)
                last_t = t
                if gap:
                    if self._cancel.wait(min(gap / self.speed, 1.2)):
                        break
                if kind == "usb":
                    if self.usb:
                        self.usb.push(payload)
                    continue
                log.write(payload)
                self.on_line(payload, kind)
        cancelled = self._cancel.is_set()
        self.on_exit(1 if cancelled else int(self.sc.get("exit_code", 1)), cancelled)

    def cancel(self):
        self._cancel.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
