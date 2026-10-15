"""The rules engine: restore attempts in, a verdict a tech can act on out.

Order of reasoning (see the spec):
  1. setup gate - PC, driver, network, signing, IPSW problems are reported as
     such and never blamed on the phone;
  2. stop point - the stage and named step the attempt died in;
  3. supporting evidence - signatures, device log text, checkpoints, panics,
     what the USB watch saw;
  4. repeat runs - the same stop twice is hardware, moving stops are the link;
  5. tech context - model, recent work, DC supply reading.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .kb import get_kb
from .logparse import STAGE_PLAIN, STAGES, AttemptSummary
from .models import DeviceModel, has_face_id, is_stacked_board, model_notes, usb_chip_name
from .panic import PanicSummary, read_panic

# Signatures that settle a run on their own, with the headline the tech sees.
STRONG_SETUP_SIGS = {
    "no_device": "The phone was never detected: rule out the cable, port and driver first",
    "usbmuxd_down": "This PC can't talk to Apple Mobile Device Service",
    "ipsw_unreadable": "Not a phone fault: the IPSW file could not be read",
    "ipsw_wrong_model": "Not a phone fault: this IPSW is not for this phone",
    "not_signed": "Not a phone fault: Apple is not signing this iOS version for this phone",
    "tss_unreachable": "Not a phone fault: Apple's signing server could not be reached",
    "normal_to_recovery": "The phone would not switch to Recovery: put it in Recovery or DFU by hand",
}


@dataclass
class UsbObservation:
    """What the USB watch saw during one attempt (None fields = no data)."""
    available: bool = False
    restore_pid_seen_while_waiting: bool = False   # phone was on USB in restore/normal mode while the engine waited
    mode_after_failure: str | None = None          # DFU | Recovery | Normal | Gone | None
    drops: int = 0


@dataclass
class Suspect:
    part: str
    label: str
    kind: str
    score: float
    reasons: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def percent(self) -> int:
        return int(round(min(self.score, 1.0) * 100))


@dataclass
class Verdict:
    category: str            # success | setup | firmware | hardware | inconclusive | cancelled
    headline: str
    summary: str
    confidence: str          # high | medium | low
    suspects: list
    next_checks: list
    stop: dict
    repeated: bool
    attempts: int
    panic: PanicSummary | None = None
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "category": self.category, "headline": self.headline, "summary": self.summary,
            "confidence": self.confidence, "repeated": self.repeated, "attempts": self.attempts,
            "stop": self.stop, "warnings": self.warnings, "next_checks": self.next_checks,
            "suspects": [
                {"part": s.part, "label": s.label, "kind": s.kind, "score": round(s.score, 3),
                 "reasons": s.reasons, "evidence": s.evidence, "checks": s.checks, "notes": s.notes}
                for s in self.suspects
            ],
            "panic": None if not self.panic or self.panic.empty else {
                "panic_string": self.panic.panic_string, "missing_sensors": self.panic.missing_sensors,
                "watchdog_daemon": self.panic.watchdog_daemon,
            },
        }


class _Board:
    def __init__(self):
        self.score = defaultdict(float)
        self.reasons = defaultdict(list)
        self.evidence = defaultdict(list)

    def add(self, part: str, w: float, reason: str, evidence: str | None = None):
        if not part:
            return
        self.score[part] += w
        if reason and reason not in self.reasons[part]:
            self.reasons[part].append(reason)
        if evidence and evidence not in self.evidence[part] and len(self.evidence[part]) < 4:
            self.evidence[part].append(evidence)


def describe_stop(a: AttemptSummary) -> dict:
    stage = a.stop_stage or a.stage
    out = {
        "stage": stage,
        "stage_no": STAGES.index(stage) + 1 if stage in STAGES else None,
        "stage_plain": STAGE_PLAIN.get(stage, stage),
        "step": a.stop_step,
        "step_plain": None,
        "part": a.stop_part,
        "progress": a.stop_progress,
        "restore_os_up": a.restore_os_up,
    }
    if a.stop_step:
        op = get_kb().operation(a.stop_step)
        out["step_plain"] = op["plain"] if op else a.stop_step
    if a.activity and a.stop_stage in ("storage", "firmware"):
        out["activity"] = a.activity
    return out


def stop_phrase(stop: dict) -> str:
    where = stop.get("activity") or stop.get("step_plain")
    if where:
        pct = stop.get("progress")
        pct_s = f", {int(pct * 100)}%" if pct is not None and 0 < pct < 1 else ""
        return f"'{where}'{pct_s}"
    return f"the '{stop.get('stage_plain')}' stage"


def observe_usb(events: list | None, a: AttemptSummary) -> UsbObservation:
    """Interpret the USB watch timeline [(t, mode), ...] for one attempt.
    Times must share the attempt's clock (seconds from its start)."""
    if events is None:
        return UsbObservation(available=False)
    obs = UsbObservation(available=True)
    obs.drops = sum(1 for _, m in events if m == "Gone")
    if a.restore_wait_t is not None and not a.restore_os_up:
        obs.restore_pid_seen_while_waiting = any(
            t >= a.restore_wait_t and m in ("Restore", "Normal") for t, m in events)
    if events:
        obs.mode_after_failure = events[-1][1]
    return obs


def diagnose(attempts: list[AttemptSummary], model: DeviceModel | None = None,
             usb: list[UsbObservation | None] | None = None, context: set | None = None,
             dc_reading: str | None = None, preflight: dict | None = None) -> Verdict:
    kb = get_kb()
    context = context or set()
    usb = usb or [None] * len(attempts)
    if len(usb) < len(attempts):
        usb = list(usb) + [None] * (len(attempts) - len(usb))
    if not attempts:
        return Verdict("inconclusive", "No restore has run yet", "", "low", [], [], {}, False, 0)

    last = attempts[-1]
    failed = [a for a in attempts if a.outcome == "failed"]
    board = _Board()
    warnings: list[str] = []
    panic = PanicSummary()

    # ---------------------------------------------------------- success
    if last.outcome == "success":
        for h in last.hits:
            if h.kind == "hardware":
                warnings.append(f"{h.title}, but the restore still finished.")
        for c in last.checkpoints_failed:
            warnings.append(f"Checkpoint '{c['name']}' reported result {c['result']}.")
        p = read_panic(last.panic_log)
        if not p.empty:
            warnings.append("The phone handed over a panic from an earlier crash: " + p.panic_string)
        head = "Restore completed"
        if any(a.outcome == "failed" for a in attempts[:-1]):
            head = "Restore completed on a later attempt"
        summary = ("The phone finished the restore. "
                   + ("Earlier attempts failed, which points to an intermittent fault or the connection."
                      if len(failed) else ""))
        return Verdict("success", head, summary.strip(), "high", [], [], describe_stop(last), False,
                       len(attempts), p if not p.empty else None, warnings)

    if last.outcome == "cancelled" and not last.hits:
        return Verdict("cancelled", "Restore cancelled", "The run was stopped before it failed.", "low",
                       [], [], describe_stop(last), False, len(attempts))

    # ---------------------------------------------------------- evidence
    decisive_setup = None
    restore_os_crash = False
    for idx, a in enumerate(attempts):
        if a.outcome not in ("failed", "cancelled"):
            continue
        f = 1.0 if a is last else 0.6
        clue_budget = defaultdict(float)
        counted = set()
        for h in a.hits:
            if h.sig_id in counted and h.kind != "clue":
                continue  # one signature counts once per run, however many lines repeat it
            counted.add(h.sig_id)
            if h.kind == "clue":
                for part, w in h.parts:
                    if clue_budget[part] < 0.3:
                        clue_budget[part] += w
                        board.add(part, w * f, h.title, h.evidence)
                continue
            for part, w in h.parts:
                board.add(part, w * f, h.title, h.evidence)
            if a is last and h.sig_id in STRONG_SETUP_SIGS and decisive_setup is None:
                decisive_setup = h

        stop = describe_stop(a)
        obs = usb[idx]
        sig_ids = {h.sig_id for h in a.hits}
        hw_sig = any(h.kind == "hardware" for h in a.hits)

        # stop inside a named restore step
        if a.stop_stage in ("storage", "firmware", "finish") and a.stop_part:
            dropped = bool({"restored_read_abort", "asr_transport"} & sig_ids) or bool(a.disconnects)
            w = 0.15 if hw_sig else (0.35 if dropped else 0.3)
            board.add(a.stop_part, w * f, f"Stopped during {stop_phrase(stop)}")
        for code in a.status_codes:
            if code not in (0, -1, 6, 14, 27, 50, 51, 53, 1015) and a.stop_part:
                board.add(a.stop_part, 0.2 * f, f"The phone reported status {code}")

        # restore OS never came up: decide between a phone crash and the PC
        pc_side = False
        if a.stop_stage == "restoreos" and not a.restore_os_up and \
                {"restore_mode_failed", "reconnected_after_personalize"} & sig_ids:
            if obs and obs.available:
                if obs.restore_pid_seen_while_waiting:
                    board.add("pc_drivers", 0.6 * f,
                              "The phone showed up on USB in restore mode but the PC could not talk to it")
                    pc_side = True
                elif obs.mode_after_failure in ("Recovery", "DFU"):
                    board.add("nand", 0.3 * f, "The phone rebooted to Recovery instead of booting the restore OS")
                    board.add("i2c_peripheral", 0.25 * f, "A crash while booting the restore OS often comes from "
                              "a peripheral holding the I2C bus")
                    board.add("early_boot", 0.15 * f, "The restore OS did not finish booting")
                else:
                    board.add("nand", 0.3 * f, "The phone disappeared while booting the restore OS")
                    board.add("i2c_peripheral", 0.25 * f, "A crash while booting the restore OS often comes from "
                              "a peripheral holding the I2C bus")
                    board.add("early_boot", 0.25 * f, "The phone disappeared from USB entirely")
            else:
                board.add("pc_drivers", 0.25 * f, "Without USB watch data, a PC driver problem can't be ruled out")
                board.add("nand", 0.25 * f, "The restore OS did not come up")
                board.add("i2c_peripheral", 0.2 * f, "The restore OS did not come up")
            if model is not None and has_face_id(model) and not pc_side:
                board.add("faceid", 0.12 * f, "Face ID model: the front sensor flex is a frequent culprit here")
            if not pc_side and a is last:
                restore_os_crash = True

        if a.stop_stage == "detect" and not a.hits:
            board.add("usb_link", 0.4 * f, "The phone was never seen")
            board.add("pc_drivers", 0.25 * f, "The phone was never seen")

        # panic handed over at the start of this attempt = the previous crash
        p = read_panic(a.panic_log)
        if not p.empty:
            panic = p
            for part, w, why in p.findings:
                board.add(part, w, why, p.panic_string)

    # ---------------------------------------------------------- repeats
    repeated = False
    if len(failed) >= 2:
        k = [(a.stop_stage, a.stop_step or a.activity) for a in failed[-2:]]
        if k[0] == k[1]:
            repeated = True
            part = failed[-1].stop_part
            if part:
                board.add(part, 0.25, f"Stopped at the same point on {len(failed)} runs")
            else:
                top_hw = _top(board, kb, kinds=("hardware",))
                if top_hw:
                    board.add(top_hw, 0.2, f"Stopped at the same point on {len(failed)} runs")
            for part in list(board.score):
                if kb.component(part).get("kind") == "setup":
                    board.score[part] *= 0.6  # a repeatable stop argues against the cable
            p1, p2 = failed[-2].stop_progress, failed[-1].stop_progress
            if p1 is not None and p2 is not None and abs(p1 - p2) < 0.05:
                board.add("nand", 0.1, "Broke off at the same percent both times")
        else:
            board.add("usb_link", 0.3, "The stop point moved between runs, which points to the connection")
            for part in list(board.score):
                if kb.component(part).get("kind") == "hardware":
                    board.score[part] *= 0.8

    # ---------------------------------------------------------- context
    for flag in kb.context_flags:
        if flag["id"] in context:
            for part, boost in flag["boost"].items():
                if board.score.get(part, 0) > 0:
                    board.add(part, boost, f"History: {flag['label'].lower()}")
    if dc_reading:
        for r in kb.dc_readings:
            if r["id"] == dc_reading:
                for p in r["parts"]:
                    board.add(p["part"], p["w"], f"DC supply: {r['label']}")
                if r.get("note"):
                    warnings.append(r["note"])

    # ---------------------------------------------------------- preflight
    for name, res in (preflight or {}).items():
        if res is not None and not res.get("ok", True):
            part = {"engine": None, "amds": "pc_drivers", "signing": "network_signing",
                    "hosts": "network_signing"}.get(name)
            if part:
                board.add(part, 0.3, res.get("label", name) + " failed the ready check")

    # ---------------------------------------------------------- verdict
    suspects = _suspects(board, kb, model)
    stop = describe_stop(last)
    hw = [s for s in suspects if s.kind == "hardware"]
    top = suspects[0] if suspects else None

    if decisive_setup is not None and (not hw or hw[0].score < 0.7):
        part = decisive_setup.parts[0][0] if decisive_setup.parts else None
        kind = kb.component(part).get("kind", "setup") if part else "setup"
        category = "firmware" if kind == "firmware" else "setup"
        headline = STRONG_SETUP_SIGS.get(decisive_setup.sig_id, decisive_setup.title)
        summary = decisive_setup.why
        confidence = "high" if max((w for _, w in decisive_setup.parts), default=0) >= 0.7 else "medium"
        suspects = [s for s in suspects if s.kind != "hardware"] + [s for s in suspects if s.kind == "hardware"]
    elif hw and hw[0].score >= 0.3:
        category = "hardware"
        lead = hw[0]
        transient_only = all(h.kind in ("transient", "info", "clue") for h in last.hits) and not last.status_codes
        if len(failed) == 1 and transient_only and lead.score < 0.6 and not panic.findings:
            category = "inconclusive"
            headline = f"Stopped at {stop_phrase(stop)}. Run it once more to confirm"
            summary = ("A single drop like this is often the cable or port. If it stops at the same point again, "
                       f"the lead suspect is: {lead.label}.")
            if restore_os_crash:
                summary += (" On the next run the phone usually hands over the panic from this crash, "
                            "which often names the part.")
            confidence = "low"
        else:
            headline = f"Stopped at {stop_phrase(stop)}. Most likely: {lead.label}"
            summary = "Why: " + " · ".join(lead.reasons[:3])
            strong_hw = any(h.kind == "hardware" and max((w for _, w in h.parts), default=0) >= 0.65
                            for h in last.hits)
            if lead.score >= 0.85 and (repeated or strong_hw):
                confidence = "high"
            elif lead.score >= 0.5:
                confidence = "medium"
            else:
                confidence = "low"
    elif top is not None:
        category = "setup" if top.kind == "setup" else ("firmware" if top.kind == "firmware" else "inconclusive")
        headline = f"Stopped at {stop_phrase(stop)}. Check first: {top.label}"
        summary = "Why: " + " · ".join(top.reasons[:3])
        confidence = "medium" if top.score >= 0.5 else "low"
    else:
        category = "inconclusive"
        headline = f"Stopped at {stop_phrase(stop)}. No clear cause in the log"
        summary = "The log gave no specific clue. Run it once more with a different cable and port."
        confidence = "low"

    next_checks = _next_checks(category, suspects, failed, repeated, confidence)
    return Verdict(category, headline, summary, confidence, suspects[:5], next_checks, stop, repeated,
                   len(attempts), panic if not panic.empty else None, warnings)


def _top(board: _Board, kb, kinds=("hardware",)) -> str | None:
    best = None
    for part, sc in board.score.items():
        if kb.component(part).get("kind") in kinds and (best is None or sc > board.score[best]):
            best = part
    return best


def _suspects(board: _Board, kb, model: DeviceModel | None) -> list[Suspect]:
    out = []
    chip = usb_chip_name(model)
    for part, sc in board.score.items():
        if sc < 0.1:
            continue
        comp = kb.component(part)
        out.append(Suspect(
            part=part, label=comp.get("label", part), kind=comp.get("kind", "hardware"), score=sc,
            reasons=board.reasons[part], evidence=board.evidence[part],
            checks=_model_checks(comp.get("checks", []), model, chip),
            notes=model_notes(model, part),
        ))
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def _model_checks(checks: list[str], model: DeviceModel | None, chip: str) -> list[str]:
    """Fill in model facts and drop checks that don't apply to this model."""
    out = []
    for c in checks:
        if c.startswith("stacked:"):
            if model is not None and is_stacked_board(model) is False:
                continue
            c = c[len("stacked:"):].strip()
        elif c.startswith("faceid:"):
            if model is not None and not has_face_id(model):
                continue
            c = c[len("faceid:"):].strip()
        out.append(c.replace("{usb_chip}", chip))
    return out


def _next_checks(category: str, suspects: list[Suspect], failed: list, repeated: bool,
                 confidence: str = "low") -> list[str]:
    checks: list[str] = []
    if category in ("hardware", "inconclusive") and len(failed) == 1 and not repeated and confidence != "high":
        checks.append("Run it once more on a different cable and a rear USB port. Stopping at the same "
                      "point again confirms the result.")
    def keep(c: str) -> bool:
        # once a stop has repeated, "run it again to confirm" advice is done
        return not (repeated and ("once more" in c or "one more run" in c))
    for s in suspects[:1]:
        checks.extend(c for c in s.checks if keep(c))
        checks.extend(s.notes)
    for s in suspects[1:3]:
        if s.checks and s.checks[0] not in checks:
            checks.append(f"Next suspect: {s.label}. {s.checks[0]}")
    seen, out = set(), []
    for c in checks:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:7]
