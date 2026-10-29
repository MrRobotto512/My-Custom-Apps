"""The session controller: one phone on the bench, its attempts, and the verdict.

UI-agnostic: it reports through ``notify(kind, payload)`` from worker threads,
and the UI drains those on its own thread.
"""
from __future__ import annotations

import bisect
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import paths
from .cases import CaseStore
from .diagnose import Verdict, describe_stop, diagnose, observe_usb, stop_phrase
from .engine import DeviceInfo, RestoreJob, probe_device
from .kb import get_kb
from .logparse import AttemptSummary, RestoreLogParser
from .report import write_report


@dataclass
class Attempt:
    n: int
    erase: bool
    ipsw: str
    folder: Path
    parser: RestoreLogParser
    job: object = None
    exit_code: int | None = None
    cancelled: bool = False
    started_mono: float = 0.0
    ended_mono: float | None = None
    marks: list = field(default_factory=list)       # (monotonic, parser_t)
    usb_rel: list | None = None

    @property
    def summary(self) -> AttemptSummary:
        return self.parser.summary()


class Session:
    def __init__(self, store: CaseStore, notify: Callable[[str, dict], None], usb_watch=None,
                 sim_id: str | None = None, settle_seconds: float = 20.0):
        self.store = store
        self.notify = notify
        self.usb = usb_watch
        self.sim_id = sim_id
        self.settle_seconds = settle_seconds if not sim_id else 0.5
        self.device = DeviceInfo()
        self.case_id: int | None = None
        self.case_folder: Path | None = None
        self.attempts: list[Attempt] = []
        self.context: set[str] = set()
        self.dc_reading: str | None = None
        self.ticket = ""
        self.notes = ""
        self.preflight: dict = {}
        self.verdict: Verdict | None = None
        self._lock = threading.Lock()

    # ----------------------------------------------------------- device
    def probe(self) -> DeviceInfo:
        if self.sim_id:
            from .sim import SimDevice
            self.device = SimDevice(self.sim_id).probe()
        else:
            self.device = probe_device()
        return self.device

    # ------------------------------------------------------------- case
    def new_case(self) -> None:
        self.case_id = None
        self.case_folder = None
        self.attempts = []
        self.verdict = None
        self.context = set()
        self.dc_reading = None
        self.ticket = ""
        self.notes = ""

    def ensure_case(self) -> int:
        if self.case_id is None:
            d = self.device
            self.case_id, self.case_folder = self.store.new_case(
                d.product, d.name, d.ecid, d.serial, self.ticket, self.notes, self.context, self.dc_reading)
            self.store.set_preflight(self.case_id, self.preflight)
        else:
            self.store.set_context(self.case_id, self.ticket, self.notes, self.context, self.dc_reading)
        return self.case_id

    @property
    def running(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].ended_mono is None

    # ---------------------------------------------------------- attempts
    def start_attempt(self, ipsw: Path | str, erase: bool) -> Attempt:
        if self.running:
            raise RuntimeError("A restore is already running")
        case_id = self.ensure_case()
        n = len(self.attempts) + 1
        folder = self.store.attempt_dir(case_id, n)
        att = Attempt(n, erase, str(ipsw), folder, RestoreLogParser())
        att.started_mono = time.monotonic()
        self.attempts.append(att)

        def on_line(line: str, stream: str) -> None:
            with self._lock:
                events = att.parser.feed(line, stream)
                att.marks.append((time.monotonic(), att.parser.last_t))
            if events:
                self.notify("events", {"attempt": n, "events": events})
            self.notify("line", {"attempt": n, "line": line.rstrip("\n"), "stream": stream})

        def on_exit(code: int, cancelled: bool) -> None:
            att.exit_code, att.cancelled = code, cancelled
            threading.Thread(target=self._finish_attempt, args=(att,), daemon=True).start()

        if self.sim_id:
            from .sim import SimJob
            job = SimJob(self.sim_id, folder, on_line, on_exit, usb=self.usb)
        else:
            cache = paths.data_dir() / "engine-cache"
            cache.mkdir(exist_ok=True)
            job = RestoreJob(Path(ipsw), erase, self.device.ecid, folder, on_line, on_exit, cache_dir=cache)
        att.job = job
        # Announce first so the UI resets its tracker before the engine's first lines arrive.
        self.notify("attempt_started", {"attempt": n, "erase": erase, "ipsw": str(ipsw)})
        try:
            job.start()
        except Exception as exc:
            # Never leave a half-started attempt behind: it would keep the session "running".
            self.attempts.remove(att)
            self.notify("attempt_aborted", {"attempt": n, "error": str(exc)})
            raise
        return att

    def cancel(self) -> None:
        if self.running and self.attempts[-1].job is not None:
            self.attempts[-1].job.cancel()

    def _settle_usb(self, att: Attempt) -> None:
        """After the engine exits, keep watching USB briefly to see where the phone goes."""
        if self.usb is None or not getattr(self.usb, "available", False):
            return
        self.notify("settling", {"attempt": att.n, "seconds": self.settle_seconds})
        deadline = time.monotonic() + self.settle_seconds
        quiet_since = time.monotonic()
        last_len = len(self.usb.timeline)
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if len(self.usb.timeline) != last_len:
                last_len = len(self.usb.timeline)
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since > min(6.0, self.settle_seconds):
                break

    def _usb_for(self, att: Attempt) -> list | None:
        if self.usb is None or not getattr(self.usb, "available", False):
            return None
        rel = []
        monos = [m for m, _ in att.marks]
        for t, mode in self.usb.since(att.started_mono):
            T = att.started_mono + t
            i = bisect.bisect_right(monos, T) - 1
            if i < 0:
                pt = 0.0
            else:
                pt = att.marks[i][1] + (T - att.marks[i][0] if i == len(monos) - 1 else 0.0)
            rel.append((round(pt, 3), mode))
        return rel

    def _finish_attempt(self, att: Attempt) -> None:
        try:
            self._finish_attempt_inner(att)
        except Exception as exc:  # noqa: BLE001 - the UI must always leave the running state
            att.ended_mono = att.ended_mono or time.monotonic()
            self.notify("attempt_finished", {"attempt": att.n, "summary": att.parser.summary(),
                                             "verdict": self.verdict, "error": str(exc)})

    def _finish_attempt_inner(self, att: Attempt) -> None:
        with self._lock:
            summary = att.parser.finish(att.exit_code, att.cancelled)
        self._settle_usb(att)
        att.usb_rel = self._usb_for(att)
        att.ended_mono = time.monotonic()
        # The engine identifies the phone more completely than the probe can in DFU.
        dev = summary.device
        if dev.get("product") and not self.device.product:
            self.device.product = dev["product"]
        if dev.get("ecid") and not self.device.ecid:
            self.device.ecid = dev["ecid"]
        if dev.get("serial") and not self.device.serial:
            self.device.serial = dev["serial"]
        if self.device.model and not self.device.name:
            self.device.name = self.device.model.name
        self.store.update_device(self.case_id, self.device.product, self.device.name, self.device.ecid,
                                 self.device.serial)
        self.store.add_attempt(self.case_id, att.n, summary, "erase" if att.erase else "update", att.ipsw,
                               att.exit_code, att.job.raw_log, att.usb_rel)
        self.verdict = self.rediagnose()
        self.notify("attempt_finished", {"attempt": att.n, "summary": summary, "verdict": self.verdict})

    # ------------------------------------------------------------ verdict
    def rediagnose(self) -> Verdict:
        done = [a for a in self.attempts if a.ended_mono is not None]
        summaries = [a.summary for a in done]
        usb = [observe_usb(a.usb_rel, a.summary) if a.usb_rel is not None else None for a in done]
        v = diagnose(summaries, self.device.model, usb=usb, context=self.context, dc_reading=self.dc_reading,
                     preflight=self.preflight)
        self.verdict = v
        if self.case_id is not None:
            self.store.set_context(self.case_id, self.ticket, self.notes, self.context, self.dc_reading)
            self.store.set_verdict(self.case_id, v, summaries[-1] if summaries else None)
            self.write_report()
        return v

    def similar_cases(self) -> list:
        if not self.verdict or self.case_id is None:
            return []
        return [dict(r) for r in self.store.similar(self.device.product, self.verdict.stop.get("stage"),
                                                    self.verdict.stop.get("step"), exclude=self.case_id)]

    def write_report(self) -> Path | None:
        if self.case_id is None or self.verdict is None:
            return None
        row = dict(self.store.get(self.case_id))
        kb = get_kb()
        labels = {f["id"]: f["label"] for f in kb.context_flags}
        row["context"] = [labels.get(c, c) for c in json.loads(row.get("context") or "[]")]
        dc = {r["id"]: r["label"] for r in kb.dc_readings}
        row["dc_reading"] = dc.get(row.get("dc_reading") or "", row.get("dc_reading"))
        atts = []
        for a in self.attempts:
            s = a.summary
            atts.append({"n": a.n, "restore_mode": "erase" if a.erase else "update", "outcome": s.outcome,
                         "stop": stop_phrase(describe_stop(s)) if s.outcome != "success" else "finished",
                         "signatures": "; ".join(dict.fromkeys(h.title for h in s.hits if h.kind != "clue"))})
        tail: list[str] = []
        if self.attempts:
            try:
                tail = Path(self.attempts[-1].job.raw_log).read_text(encoding="utf-8",
                                                                      errors="replace").splitlines()[-80:]
            except OSError:
                pass
        return write_report(Path(row["folder"]), row, self.verdict, atts, tail, self.similar_cases())
