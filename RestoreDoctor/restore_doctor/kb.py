"""Loads the knowledge base JSON files once and hands out compiled views."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .paths import resource_dir


def _load(name: str) -> Any:
    path = resource_dir() / "kb" / name
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Signature:
    id: str
    regex: re.Pattern
    kind: str
    parts: tuple[tuple[str, float], ...]
    title: str
    why: str
    stop_stage: str | None = None


@dataclass(frozen=True)
class Activity:
    regex: re.Pattern
    plain: str
    stage: str
    part: str | None


@dataclass(frozen=True)
class Keyword:
    regex: re.Pattern
    part: str


@dataclass
class KnowledgeBase:
    operations: dict[str, dict]
    components: dict[str, dict]
    signatures: list[Signature]
    activities: list[Activity]
    keywords: list[Keyword]
    apple_codes: list[dict]
    dc_readings: list[dict]
    context_flags: list[dict]
    devices: list[dict]
    _by_code: dict[int, str] = field(default_factory=dict)

    def operation(self, name: str) -> dict | None:
        return self.operations.get(name.strip())

    def operation_by_code(self, code: int) -> tuple[str, dict] | None:
        name = self._by_code.get(code)
        return (name, self.operations[name]) if name else None

    def component(self, key: str) -> dict:
        return self.components.get(key) or {"kind": "hardware", "label": key, "checks": []}

    def part_label(self, key: str) -> str:
        return self.component(key).get("label", key)

    def lookup_apple_code(self, code: int) -> dict | None:
        for entry in self.apple_codes:
            for c in entry["codes"]:
                if isinstance(c, list) and c[0] <= code <= c[1]:
                    return entry
                if c == code:
                    return entry
        return None


@lru_cache(maxsize=1)
def get_kb() -> KnowledgeBase:
    ops = {k: v for k, v in _load("operations.json").items() if not k.startswith("_")}
    comps = {k: v for k, v in _load("components.json").items() if not k.startswith("_")}
    sig_doc = _load("signatures.json")
    sigs = [
        Signature(
            id=s["id"],
            regex=re.compile(s["regex"], re.IGNORECASE | re.MULTILINE),
            kind=s["kind"],
            parts=tuple((p["part"], float(p["w"])) for p in s.get("parts", [])),
            title=s["title"],
            why=s["why"],
            stop_stage=s.get("stop_stage"),
        )
        for s in sig_doc["signatures"]
    ]
    acts = [
        Activity(re.compile(a["regex"], re.IGNORECASE), a["plain"], a["stage"], a.get("part"))
        for a in sig_doc.get("activities", [])
    ]
    kws = [Keyword(re.compile(k["regex"]), k["part"]) for k in sig_doc.get("log_keywords", [])]
    bench = _load("bench.json")
    kb = KnowledgeBase(
        operations=ops,
        components=comps,
        signatures=sigs,
        activities=acts,
        keywords=kws,
        apple_codes=_load("apple_codes.json")["entries"],
        dc_readings=bench["dc_readings"],
        context_flags=bench["context_flags"],
        devices=_load("devices.json")["devices"],
    )
    kb._by_code = {v["code"]: k for k, v in ops.items()}
    return kb
