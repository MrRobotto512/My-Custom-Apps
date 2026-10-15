"""A small reader for iOS panic text.

When a restore attempt ends in a crash, the restore OS on the *next* attempt
hands over the panic from that crash (idevicerestore logs it as "Panic log is
available"). This module pulls out the parts of it a tech acts on.

It is deliberately conservative: it names a part only for patterns that are
well established, and otherwise shows the raw clues. Model-specific sensor
codes belong in the BenchTool iOS Panic Diagnoser, which this is meant to feed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS = [
    (re.compile(r"\bi2c\b|AppleS5LI2C|I2C", re.I), "i2c_peripheral", 0.3, "an I2C bus fault"),
    (re.compile(r"AppleANS|ANS2|AppleNVMe|nvme|NAND", re.I), "nand", 0.35, "the storage controller (ANS/NVMe)"),
    (re.compile(r"SEP (Panic|ROM)|AppleSEP", re.I), "sep", 0.3, "the Secure Enclave"),
    (re.compile(r"AppleBaseband|baseband|BBUpdater", re.I), "baseband", 0.3, "the baseband"),
    (re.compile(r"\bTG0B\b|\bTG0V\b|battery|gas ?gauge|charger", re.I), "battery", 0.25, "the battery or its sensors"),
]


@dataclass
class PanicSummary:
    panic_string: str = ""
    missing_sensors: list = field(default_factory=list)
    watchdog_daemon: str | None = None
    findings: list = field(default_factory=list)   # (part, weight, why)

    @property
    def empty(self) -> bool:
        return not (self.panic_string or self.missing_sensors or self.findings)


def read_panic(lines: list[str]) -> PanicSummary:
    text = "\n".join(lines)
    out = PanicSummary()
    if not text.strip():
        return out
    m = re.search(r"panic\(cpu \d+ caller [^)]*\):\s*(.+)", text)
    if m:
        out.panic_string = m.group(1).strip()[:240]
    else:
        first = next((ln.strip() for ln in lines if ln.strip()), "")
        out.panic_string = first[:240]
    m = re.search(r"Missing sensor\(s\):\s*([A-Za-z0-9_, ]+)", text)
    if m:
        out.missing_sensors = [x.strip() for x in m.group(1).replace(",", " ").split() if x.strip()]
    m = re.search(r"no successful checkins from (\S+)", text)
    if m:
        out.watchdog_daemon = m.group(1)
    seen = set()
    for rx, part, w, why in _PATTERNS:
        if rx.search(text) and part not in seen:
            seen.add(part)
            out.findings.append((part, w, f"The panic from the previous attempt mentions {why}."))
    return out
