"""Turns idevicerestore output into structured restore events.

The engine is run with ``--logfile=- -d -P``:
  * stderr carries the full log, one record per line as
        HH:MM:SS.mmm <Level> message
    with continuation lines (plist dumps, device logs, backtraces, libtatsu
    errors) printed without a timestamp;
  * stdout carries INFO-level copies of the same messages plus plain progress
    lines ("progress: <step> <fraction>", "Uploading:  45.2").

The parser is incremental: feed it lines as they arrive and read
``summary()`` at any time. It also accepts whole log files (Import log).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .kb import get_kb

STAGES = ["detect", "sign", "bootchain", "restoreos", "storage", "firmware", "finish"]
STAGE_PLAIN = {
    "detect": "Detect the phone",
    "sign": "Get Apple's signature",
    "bootchain": "Load the boot chain",
    "restoreos": "Boot the restore OS",
    "storage": "Prepare and write storage",
    "firmware": "Flash firmware",
    "finish": "Finish",
}

# idevicerestore's coarse progress steps (RESTORE_STEP_* in idevicerestore.h)
PROGRESS_STEPS = {
    0: ("detect", "Detecting the phone"),
    1: ("bootchain", "Preparing the restore"),
    2: ("storage", "Writing iOS to storage"),
    3: ("storage", "Verifying what was written"),
    4: ("firmware", "Flashing firmware"),
    5: ("firmware", "Updating the baseband"),
    6: ("firmware", "Updating peripheral firmware"),
    7: ("restoreos", "Uploading images"),
}

TS_LINE = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3})\s+<\s*(Error|Warning|Notice|Info|Verbose|Debug)>\s?(.*)$")
PROGRESS_LINE = re.compile(r"^progress:\s+(\d+)\s+([0-9.]+)\s*$")
TAGGED_PROGRESS = re.compile(r"^([A-Za-z][A-Za-z ]*):\s+([0-9.]+)\s*$")
OP_LINE = re.compile(r"^(.+?) \((\d+)\)\s*$")

MODE_WORDS = {
    "dfu": "DFU", "recovery": "Recovery", "normal": "Normal", "restore": "Restore",
    "wtf": "WTF", "port dfu": "Port DFU", "unknown": "Unknown",
}


def _mode(word: str) -> str:
    return MODE_WORDS.get(word.strip().lower(), word.strip())


@dataclass
class Hit:
    sig_id: str
    kind: str
    title: str
    why: str
    parts: tuple
    evidence: str
    stage: str
    step: str | None
    t: float


@dataclass
class Event:
    t: float
    kind: str
    data: dict


@dataclass
class AttemptSummary:
    outcome: str = "running"            # running | success | failed | cancelled
    stage: str = "detect"               # stage reached so far
    max_stage: str = "detect"
    step: str | None = None             # last restore operation name (engine wording)
    step_plain: str | None = None
    step_part: str | None = None
    activity: str | None = None         # last data-request activity (plain)
    activity_part: str | None = None
    progress_step: int | None = None
    progress: float | None = None       # 0..1 within progress_step
    status_codes: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    checkpoints_failed: list = field(default_factory=list)
    device_log: list = field(default_factory=list)
    previous_log: list = field(default_factory=list)
    backtrace: list = field(default_factory=list)
    panic_log: list = field(default_factory=list)   # previous boot's panic (SavedDebugInfo)
    usb_log: list = field(default_factory=list)
    modes: list = field(default_factory=list)       # (t, mode) seen by the engine
    disconnects: list = field(default_factory=list)  # t
    restore_os_up: bool = False
    restore_mode_wait: bool = False
    restore_wait_t: float | None = None
    device: dict = field(default_factory=dict)
    restore_kind: str | None = None     # update | erase
    errors: list = field(default_factory=list)      # engine <Error> lines
    stop_stage: str | None = None
    stop_step: str | None = None
    stop_part: str | None = None
    stop_progress: float | None = None
    started: float | None = None
    ended: float | None = None
    files: list = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"


class RestoreLogParser:
    """Incremental parser. ``feed`` returns the events produced by a line."""

    def __init__(self, clock=time.monotonic):
        self.kb = get_kb()
        self._clock = clock
        self._t0: float | None = None
        self._ts0: int | None = None          # first timestamp (ms of day)
        self._seen_stderr_ts = False
        self.s = AttemptSummary()
        self.events: list[Event] = []
        self._block: str | None = None        # 'devlog' | 'prevlog' | 'backtrace' | None
        self._ckpt_names: dict[str, str] = {}
        self.last_t = 0.0

    # ------------------------------------------------------------------ time
    def _now(self, ts_ms: int | None = None) -> float:
        """Seconds since the first log line. Timestamped lines carry their own
        time; untimestamped lines (progress, continuations) take the time of
        the last timestamped line, so one attempt has a single time base."""
        if ts_ms is not None:
            if self._ts0 is None:
                self._ts0 = ts_ms
            delta = ts_ms - self._ts0
            if delta < 0:  # past midnight
                delta += 24 * 3600 * 1000
            self.last_t = delta / 1000.0
            return self.last_t
        if self._ts0 is None:
            # no timestamps at all (stdout-only import): fall back to the clock
            if self._t0 is None:
                self._t0 = self._clock()
            self.last_t = self._clock() - self._t0
        return self.last_t

    def _emit(self, t: float, kind: str, **data) -> Event:
        ev = Event(t, kind, data)
        self.events.append(ev)
        return ev

    # ----------------------------------------------------------------- input
    def feed(self, line: str, stream: str = "err") -> list[Event]:
        """stream: 'err' (engine stderr log), 'out' (engine stdout), 'file' (imported log)."""
        start = len(self.events)
        line = line.rstrip("\r\n")
        if not line.strip():
            return []
        m = PROGRESS_LINE.match(line)
        if m:
            self._on_progress(self._now(), int(m.group(1)), float(m.group(2)))
            return self.events[start:]
        m = TS_LINE.match(line)
        if m:
            if stream == "err":
                self._seen_stderr_ts = True
            hh, mm, ss, ms, level, msg = m.groups()
            ts_ms = ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(ms)
            t = self._now(ts_ms)
            if self._block == "backtrace" and msg.startswith("\t"):
                # backtrace frames are logged one per line, indented
                self.s.backtrace.append(msg.strip())
                self._keywords(t, msg.strip(), source="crash backtrace")
                return self.events[start:]
            self._block = None
            self._message(t, level, msg)
            return self.events[start:]
        if stream == "out":
            if TAGGED_PROGRESS.match(line):
                return []
            if self._seen_stderr_ts:
                return []  # stdout repeats what stderr already logged
            self._message(self._now(), "Info", line)
            return self.events[start:]
        # untimestamped stderr / file line: continuation, or a libtatsu message
        self._continuation(self._now(), line)
        return self.events[start:]

    def feed_text(self, text: str, stream: str = "file") -> None:
        for ln in text.splitlines():
            self.feed(ln, stream)

    # -------------------------------------------------------------- handlers
    def _set_stage(self, t: float, stage: str) -> None:
        s = self.s
        if stage == s.stage:
            return
        s.stage = stage
        if STAGES.index(stage) > STAGES.index(s.max_stage):
            s.max_stage = stage
        self._emit(t, "stage", stage=stage, plain=STAGE_PLAIN[stage])

    def _on_progress(self, t: float, step: int, frac: float) -> None:
        s = self.s
        s.progress_step, s.progress = step, frac
        stage, plain = PROGRESS_STEPS.get(step, (None, None))
        if stage and STAGES.index(stage) >= STAGES.index(s.stage) and step in (2, 3, 4, 5, 6):
            self._set_stage(t, stage)
        self._emit(t, "progress", step=step, fraction=frac, plain=plain)

    def _continuation(self, t: float, line: str) -> None:
        s = self.s
        text = line.strip()
        if self._block == "devlog":
            s.device_log.append(text)
            self._keywords(t, text, source="device log")
        elif self._block == "prevlog":
            s.previous_log.append(text)
        elif self._block == "backtrace":
            s.backtrace.append(text)
        elif self._block == "panic":
            s.panic_log.append(line.rstrip())
            return  # panic text is analysed as a whole by the panic reader
        elif self._block == "usblog":
            s.usb_log.append(text)
            return
        self._signatures(t, text)

    def _message(self, t: float, level: str, msg: str) -> None:
        s = self.s
        if s.started is None:
            s.started = t
        s.ended = t
        text = msg.strip()
        if level == "Error":
            s.errors.append(text)

        # --- multi-line blocks
        if text.startswith("Log is available:"):
            self._block = "devlog"
            rest = text[len("Log is available:"):].strip()
            if rest:
                s.device_log.append(rest)
            self._emit(t, "device_log")
            return
        if text.startswith("Previous Restore Log Received:"):
            self._block = "prevlog"
            return
        if text.startswith("Panic log is available:"):
            # SavedDebugInfo: the panic from the phone's previous boot, i.e.
            # usually the crash that ended the last attempt.
            self._block = "panic"
            rest = text[len("Panic log is available:"):].strip()
            if rest:
                s.panic_log.append(rest)
            self._emit(t, "panic_log")
            return
        if text.startswith("USB log is available:"):
            self._block = "usblog"
            return
        m = re.match(r"Previous restore exit status: (.+)", text)
        if m:
            s.device["previous_exit_status"] = m.group(1)
        if "restored crashed, backtrace following" in text:
            self._block = "backtrace"

        # --- device identity
        m = re.match(r"Found device in (.+?) mode", text)
        if m:
            mode = _mode(m.group(1))
            s.modes.append((t, mode))
            s.device["mode"] = mode
            self._emit(t, "mode", mode=mode, source="engine")
        m = re.match(r"ECID: (\d+)$", text)
        if m:
            s.device["ecid"] = int(m.group(1))
        m = re.match(r"Identified device as (\S+), (\S+)", text)
        if m:
            s.device["board"], s.device["product"] = m.group(1), m.group(2)
            self._emit(t, "device", **s.device)
        m = re.match(r"Device Product Version: (.+)", text)
        if m and m.group(1) != "N/A":
            s.device["ios"] = m.group(1)
        m = re.match(r"IPSW Product Version: (.+)", text)
        if m:
            s.device["ipsw_version"] = m.group(1)
        m = re.match(r"IPSW Product Build: (\S+)", text)
        if m:
            s.device["ipsw_build"] = m.group(1)
        m = re.match(r"INFO: device serial number is (\S+)", text)
        if m:
            s.device["serial"] = m.group(1)
        m = re.match(r"iBoot build-version=(.+)", text)
        if m:
            s.device["iboot"] = m.group(1)
        m = re.match(r"radio-error(-string)?=(.+)", text)
        if m:
            s.device["radio_error" + ("_string" if m.group(1) else "")] = m.group(2)
        if text.startswith("This restore will update the device without erasing"):
            s.restore_kind = "update"
        elif text.startswith("This restore will erase all device data"):
            s.restore_kind = "erase"
        m = re.match(r"Wrote baseband updater output data to (.+)", text)
        if m:
            s.files.append(m.group(1))

        # --- engine-level mode events (debug)
        m = re.search(r"connected in (.+?) mode", text)
        if m and "_event_cb" in text:
            mode = _mode(m.group(1))
            s.modes.append((t, mode))
            self._emit(t, "mode", mode=mode, source="engine")
            if mode == "Restore":
                s.restore_mode_wait = False
        elif re.search(r"_event_cb: device .* disconnected", text):
            s.disconnects.append(t)
            s.modes.append((t, "Gone"))
            self._emit(t, "mode", mode="Gone", source="engine")

        # --- stage markers
        if text.startswith("Extracting BuildManifest from IPSW") or text.startswith("Checking IPSW for required"):
            self._set_stage(t, "detect")
        elif (text.startswith("Getting ApNonce") or text.startswith("Getting SepNonce")
              or text.startswith("Trying to fetch new SHSH") or text.startswith("Sending TSS request")
              or text.startswith("Received SHSH blobs")):
            if STAGES.index(s.stage) <= STAGES.index("sign"):
                self._set_stage(t, "sign")
        elif (text.startswith("Entering recovery mode") or text.startswith("Recovery Mode Environment")
              or re.match(r"Sending (iBSS|iBEC|iBoot|LLB|APTicket)", text)):
            if STAGES.index(s.stage) <= STAGES.index("bootchain"):
                self._set_stage(t, "bootchain")
        elif (text.startswith("Waiting for device to enter restore mode")
              or re.match(r"Sending (RestoreRamDisk|RestoreKernelCache|RestoreDeviceTree|RestoreTrustCache)", text)):
            self._set_stage(t, "restoreos")
            if text.startswith("Waiting for device to enter restore mode"):
                s.restore_mode_wait = True
                s.restore_wait_t = t
        elif text.startswith("Connected to com.apple.mobile.restored") or text.startswith("About to restore device"):
            s.restore_os_up = True
            s.restore_mode_wait = False
            if STAGES.index(s.stage) < STAGES.index("restoreos"):
                self._set_stage(t, "restoreos")
            self._emit(t, "restore_os_up")

        # --- restore operations
        m = OP_LINE.match(text)
        if m:
            name = m.group(1).strip()
            op = self.kb.operation(name)
            if op is None:
                found = self.kb.operation_by_code(int(m.group(2)))
                if found and name == "Unknown operation":
                    name, op = found
            if op is not None:
                s.step, s.step_plain, s.step_part = name, op["plain"], op.get("part")
                s.activity, s.activity_part = None, None
                self._set_stage(t, op["stage"])
                self._emit(t, "step", name=name, plain=op["plain"], stage=op["stage"], part=op.get("part"),
                           code=int(m.group(2)))

        # --- data request activities
        for act in self.kb.activities:
            if act.regex.search(text):
                s.activity, s.activity_part = act.plain, act.part
                if STAGES.index(act.stage) >= STAGES.index("restoreos"):
                    self._set_stage(t, act.stage)
                self._emit(t, "activity", plain=act.plain, stage=act.stage, part=act.part)
                break

        # --- status
        m = re.match(r"Unhandled status message \((\d+)\)", text)
        if m:
            s.status_codes.append(int(m.group(1)))
            self._emit(t, "status", code=int(m.group(1)), text=text)
        status_map = {
            "Status: Restore Finished": 0, "Status: Verification Error": -1, "Status: Disk Failure": 6,
            "Status: Fail": 14, "Status: Failed to mount filesystems.": 27,
            "Status: Failed to load SEP Firmware.": 50, "Status: Failed to recover FDR data.": 53,
            "Status: X-Gold Baseband Update Failed. Defective Unit?": 1015,
        }
        if text in status_map:
            code = status_map[text]
            s.status_codes.append(code)
            self._emit(t, "status", code=code, text=text)

        # --- checkpoints
        m = re.match(r"Checkpoint started\s+id: (0x[0-9A-Fa-f]+) \((.*)\)", text)
        if m:
            self._ckpt_names[m.group(1).upper()] = m.group(2)
        m = re.match(r"Checkpoint FAILURE id: (0x[0-9A-Fa-f]+) result=(-?\d+): ?(.*)", text)
        if m:
            name = self._ckpt_names.get(m.group(1).upper(), "unknown")
            entry = {"id": m.group(1), "name": name, "result": int(m.group(2)), "message": m.group(3), "t": t}
            s.checkpoints_failed.append(entry)
            self._keywords(t, f"{name} {m.group(3)}", source="checkpoint")
        m = re.match(r"Checkpoint completed id: (0x[0-9A-Fa-f]+) \((.*)\) result=(-?\d+)", text)
        if m and int(m.group(3)) != 0:
            entry = {"id": m.group(1), "name": m.group(2), "result": int(m.group(3)), "message": "", "t": t}
            s.checkpoints_failed.append(entry)
            self._keywords(t, m.group(2), source="checkpoint")

        # --- outcome
        if text == "DONE":
            s.outcome = "success"
            self._set_stage(t, "finish")
            self._emit(t, "outcome", outcome="success")
        elif text == "RESTORE FAILED":
            self._fail(t)

        self._signatures(t, text)

    def _keywords(self, t: float, text: str, source: str) -> None:
        for kw in self.kb.keywords:
            if kw.regex.search(text):
                self.s.hits.append(Hit(
                    sig_id=f"kw:{kw.part}", kind="clue", title=f"{source.capitalize()} mentions {kw.part}",
                    why=f"The {source} text points at this part.", parts=((kw.part, 0.12),),
                    evidence=text[:300], stage=self.s.stage, step=self.s.step, t=t))

    def _signatures(self, t: float, text: str) -> None:
        s = self.s
        for sig in self.kb.signatures:
            if sig.regex.search(text):
                if any(h.sig_id == sig.id and h.evidence == text for h in s.hits):
                    continue
                stage = s.stage
                # Before the restore OS is up the engine's own message says
                # which stage failed; after that, the phone's current step does.
                if sig.stop_stage and not s.restore_os_up \
                        and STAGES.index(sig.stop_stage) <= STAGES.index("restoreos"):
                    stage = sig.stop_stage
                hit = Hit(sig.id, sig.kind, sig.title, sig.why, sig.parts, text[:300], stage, s.step, t)
                s.hits.append(hit)
                self._emit(t, "signature", id=sig.id, sig_kind=sig.kind, title=sig.title, evidence=text[:300])

    def _fail(self, t: float) -> None:
        s = self.s
        if s.outcome == "failed":
            return
        s.outcome = "failed"
        self.freeze_stop()
        self._emit(t, "outcome", outcome="failed")

    def freeze_stop(self) -> None:
        """Record where the attempt stopped (called on failure or cancel)."""
        s = self.s
        if s.stop_stage is not None:
            return
        s.stop_stage = s.stage
        # A setup/firmware/transient signature placed earlier than the current
        # stage tells us where it really stopped (e.g. no device -> detect).
        decisive = [h for h in s.hits if h.kind in ("setup", "firmware", "transient")]
        if decisive and not s.restore_os_up:
            first = decisive[0]
            if STAGES.index(first.stage) < STAGES.index(s.stop_stage):
                s.stop_stage = first.stage
        s.stop_step = s.step if s.stop_stage in ("storage", "firmware", "finish") else None
        s.stop_part = (s.activity_part or s.step_part) if s.stop_step else None
        if s.progress_step in (2, 3) and s.stop_stage == "storage":
            s.stop_progress = s.progress

    def finish(self, exit_code: int | None, cancelled: bool = False) -> AttemptSummary:
        """Call once the engine process has exited."""
        s = self.s
        t = self._now()
        if cancelled:
            s.outcome = "cancelled"
            self.freeze_stop()
        elif s.outcome == "running":
            if exit_code == 0:
                s.outcome = "success"
                self._set_stage(t, "finish")
            else:
                self._fail(t)
        return s

    def summary(self) -> AttemptSummary:
        return self.s
