"""Case history: every phone job, every restore attempt, and the fix that worked.

Stored in SQLite under the user's app data folder, plus one folder per case
holding each attempt's raw engine log and the HTML report. The raw logs are
the source of truth: a case can always be re-diagnosed from them when the
rules improve.
"""
from __future__ import annotations

import functools
import json
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .logparse import AttemptSummary, RestoreLogParser

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created TEXT, updated TEXT,
  ecid TEXT, product TEXT, model_name TEXT, serial TEXT,
  ticket TEXT, notes TEXT, context TEXT, dc_reading TEXT,
  category TEXT, headline TEXT, top_part TEXT, confidence TEXT,
  stop_stage TEXT, stop_step TEXT, outcome TEXT,
  verdict_json TEXT,
  fix_part TEXT, fix_notes TEXT,
  folder TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER REFERENCES cases(id),
  n INTEGER, started TEXT, ended TEXT,
  restore_mode TEXT, ipsw TEXT, exit_code INTEGER, outcome TEXT,
  stop_stage TEXT, stop_step TEXT, stop_part TEXT,
  status_codes TEXT, signatures TEXT,
  log_path TEXT, usb_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_product ON cases(product);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _locked(fn):
    """The UI thread and the attempt-finishing worker share one connection."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


@dataclass
class CaseRow:
    id: int
    created: str
    product: str
    model_name: str
    serial: str
    ticket: str
    category: str
    headline: str
    top_part: str
    confidence: str
    stop_step: str
    outcome: str
    fix_part: str
    fix_notes: str
    folder: str
    attempts: int


class CaseStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or (paths.data_dir() / "cases.db")
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.db.executescript(SCHEMA)
        # Columns added after 0.1: older databases get them here.
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(cases)")}
        for col in ("preflight", "checks_done"):
            if col not in have:
                self.db.execute(f"ALTER TABLE cases ADD COLUMN {col} TEXT")
        self.db.commit()

    # -------------------------------------------------------------- writes
    @_locked
    def new_case(self, product: str | None, model_name: str | None, ecid: int | None,
                 serial: str | None = None, ticket: str = "", notes: str = "",
                 context: set | None = None, dc_reading: str | None = None) -> tuple[int, Path]:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = paths.cases_dir() / f"{stamp}-{(product or 'unknown').replace(',', '_')}"
        folder.mkdir(parents=True, exist_ok=True)
        cur = self.db.execute(
            "INSERT INTO cases (created, updated, ecid, product, model_name, serial, ticket, notes, context,"
            " dc_reading, outcome, folder) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), _now(), hex(ecid) if ecid else None, product, model_name, serial, ticket, notes,
             json.dumps(sorted(context or [])), dc_reading, "running", str(folder)))
        self.db.commit()
        return cur.lastrowid, folder

    @_locked
    def attempt_dir(self, case_id: int, n: int) -> Path:
        folder = Path(self.get(case_id)["folder"]) / f"attempt-{n}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @_locked
    def add_attempt(self, case_id: int, n: int, a: AttemptSummary, restore_mode: str, ipsw: str,
                    exit_code: int | None, log_path: Path, usb: list | None) -> None:
        self.db.execute(
            "INSERT INTO attempts (case_id, n, started, ended, restore_mode, ipsw, exit_code, outcome,"
            " stop_stage, stop_step, stop_part, status_codes, signatures, log_path, usb_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, n, None, _now(), restore_mode, ipsw, exit_code, a.outcome, a.stop_stage, a.stop_step,
             a.stop_part, json.dumps(a.status_codes), json.dumps(sorted({h.sig_id for h in a.hits})),
             str(log_path), json.dumps(usb) if usb is not None else None))
        self.db.commit()

    @_locked
    def set_verdict(self, case_id: int, verdict, last: AttemptSummary | None) -> None:
        top = verdict.suspects[0].part if verdict.suspects else None
        self.db.execute(
            "UPDATE cases SET updated=?, category=?, headline=?, top_part=?, confidence=?, stop_stage=?,"
            " stop_step=?, outcome=?, verdict_json=? WHERE id=?",
            (_now(), verdict.category, verdict.headline, top, verdict.confidence,
             verdict.stop.get("stage"), verdict.stop.get("step"),
             last.outcome if last else None, json.dumps(verdict.as_dict()), case_id))
        self.db.commit()

    @_locked
    def update_device(self, case_id: int, product: str | None, model_name: str | None,
                      ecid: int | None, serial: str | None) -> None:
        row = self.get(case_id)
        self.db.execute("UPDATE cases SET product=?, model_name=?, ecid=?, serial=? WHERE id=?",
                        (product or row["product"], model_name or row["model_name"],
                         hex(ecid) if ecid else row["ecid"], serial or row["serial"], case_id))
        self.db.commit()

    @_locked
    def set_context(self, case_id: int, ticket: str, notes: str, context: set, dc_reading: str | None):
        self.db.execute("UPDATE cases SET ticket=?, notes=?, context=?, dc_reading=? WHERE id=?",
                        (ticket, notes, json.dumps(sorted(context)), dc_reading, case_id))
        self.db.commit()

    @_locked
    def set_preflight(self, case_id: int, preflight: dict) -> None:
        """The ready check the case ran under, so re-diagnosing it later weighs it the same way."""
        self.db.execute("UPDATE cases SET preflight=? WHERE id=?", (json.dumps(preflight), case_id))
        self.db.commit()

    @_locked
    def set_checks_done(self, case_id: int, done: list[str]) -> None:
        self.db.execute("UPDATE cases SET checks_done=? WHERE id=?", (json.dumps(done), case_id))
        self.db.commit()

    @_locked
    def record_fix(self, case_id: int, part: str, notes: str) -> None:
        self.db.execute("UPDATE cases SET fix_part=?, fix_notes=?, updated=? WHERE id=?",
                        (part, notes, _now(), case_id))
        self.db.commit()

    @_locked
    def delete(self, case_id: int, remove_folder: bool = True) -> None:
        row = self.get(case_id)
        self.db.execute("DELETE FROM attempts WHERE case_id=?", (case_id,))
        self.db.execute("DELETE FROM cases WHERE id=?", (case_id,))
        self.db.commit()
        if remove_folder and row is not None and row["folder"]:
            folder = Path(row["folder"])
            # only ever remove a folder inside our own cases directory
            if folder.is_dir() and paths.cases_dir().resolve() in folder.resolve().parents:
                shutil.rmtree(folder, ignore_errors=True)

    # --------------------------------------------------------------- reads
    @_locked
    def get(self, case_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()

    @_locked
    def attempts(self, case_id: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM attempts WHERE case_id=? ORDER BY n", (case_id,)).fetchall()

    @_locked
    def list(self, search: str = "", limit: int = 300) -> list[CaseRow]:
        q = ("SELECT c.*, (SELECT COUNT(*) FROM attempts a WHERE a.case_id=c.id) AS n_att FROM cases c")
        args: list = []
        if search:
            q += (" WHERE c.product LIKE ? OR c.model_name LIKE ? OR c.serial LIKE ? OR c.ticket LIKE ?"
                  " OR c.headline LIKE ? OR c.fix_part LIKE ? OR c.notes LIKE ?")
            args = [f"%{search}%"] * 7
        q += " ORDER BY c.id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.db.execute(q, args).fetchall():
            out.append(CaseRow(r["id"], r["created"] or "", r["product"] or "", r["model_name"] or "",
                               r["serial"] or "", r["ticket"] or "", r["category"] or "", r["headline"] or "",
                               r["top_part"] or "", r["confidence"] or "", r["stop_step"] or "",
                               r["outcome"] or "", r["fix_part"] or "", r["fix_notes"] or "", r["folder"] or "",
                               r["n_att"]))
        return out

    @_locked
    def similar(self, product: str | None, stop_stage: str | None, stop_step: str | None,
                exclude: int | None = None, limit: int = 5) -> list[sqlite3.Row]:
        """Earlier cases of the same model that stopped at the same point and have a recorded fix."""
        if not product or not stop_stage:
            return []
        rows = self.db.execute(
            "SELECT * FROM cases WHERE product=? AND stop_stage=? AND IFNULL(stop_step,'')=IFNULL(?, '')"
            " AND fix_part IS NOT NULL AND fix_part<>'' AND id<>? ORDER BY id DESC LIMIT ?",
            (product, stop_stage, stop_step, exclude or -1, limit)).fetchall()
        return rows

    @_locked
    def fix_stats(self, product: str | None, stop_stage: str | None, stop_step: str | None) -> dict:
        """How often each confirmed fix resolved this model + stop point."""
        if not product or not stop_stage:
            return {}
        rows = self.db.execute(
            "SELECT fix_part, COUNT(*) AS n FROM cases WHERE product=? AND stop_stage=?"
            " AND IFNULL(stop_step,'')=IFNULL(?, '') AND fix_part IS NOT NULL AND fix_part<>''"
            " GROUP BY fix_part ORDER BY n DESC", (product, stop_stage, stop_step)).fetchall()
        return {r["fix_part"]: r["n"] for r in rows}

    # ------------------------------------------------------- re-diagnosis
    def reload_attempts(self, case_id: int) -> tuple[list[AttemptSummary], list]:
        """Re-parse each attempt's raw log (so improved rules apply to old cases)."""
        summaries, usb = [], []
        for row in self.attempts(case_id):
            p = RestoreLogParser(clock=lambda: 0.0)
            path = Path(row["log_path"] or "")
            if path.exists():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    p.feed(line, "out" if line.startswith("progress:") else "err")
            a = p.finish(row["exit_code"], cancelled=row["outcome"] == "cancelled")
            summaries.append(a)
            usb.append(json.loads(row["usb_json"]) if row["usb_json"] else None)
        return summaries, usb
