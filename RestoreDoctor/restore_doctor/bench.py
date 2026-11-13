"""Report into the shop's Hero Dashboard (BenchTool), when there is one.

Every finished attempt, verdict and recorded fix posts the whole case —
device, attempts, suspects and the HTML report — to the bench, and during a
restore a small live beat carries the stage, step and progress bar, so the
job can be watched from any browser in the shop at

    http://<bench>/restoredoctor

Entirely optional and entirely fail-silent: with no bench on the network
every post dies quietly in a daemon thread and Restore Doctor behaves as if
this module did not exist. Nothing here ever blocks the UI or a restore.

Configuration (first hit wins):
  * RD_BENCH environment variable — a URL, or "off" to disable
  * dashboard.json in the data folder:  {"url": "http://192.168.86.67:8377"}
                                    {"url": "off"} disables
  * default: http://benchtool.local:8377 (the bench's mDNS name)

Stdlib only (urllib): the PyInstaller build gains no dependency.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from . import paths

DEFAULT_URL = "http://benchtool.local:8377"
TIMEOUT_S = 6
LIVE_EVERY_S = 2.5

_lock = threading.Lock()
_last_live = 0.0
_url_cache: str | None = None


def bench_url() -> str:
    """The bench's base URL, '' when reporting is off."""
    global _url_cache
    if _url_cache is not None:
        return _url_cache
    url = (os.environ.get("RD_BENCH") or "").strip()
    if not url:
        try:
            cfg = json.loads((paths.data_dir() / "dashboard.json").read_text(encoding="utf-8"))
            url = str(cfg.get("url") or "").strip()
        except Exception:  # noqa: BLE001 - absent/broken config means default
            url = ""
    if not url:
        url = DEFAULT_URL
    if url.lower() in ("off", "0", "none", "no"):
        url = ""
    _url_cache = url.rstrip("/")
    return _url_cache


def _post(path: str, body: dict) -> None:
    url = bench_url()
    if not url:
        return
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url + path, data=data,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "RestoreDoctor"})
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT_S).read()
    except Exception:  # noqa: BLE001 - no bench, no problem
        pass


def _spawn(path: str, body: dict) -> None:
    threading.Thread(target=_post, args=(path, body), daemon=True).start()


# ------------------------------------------------------------------ the case
def case_key(row: dict) -> str:
    """One stable key per phone case across posts: serial (or ECID) + local id."""
    tag = (row.get("serial") or row.get("ecid") or "case").replace(":", "")
    tag = "".join(c for c in str(tag) if c.isalnum() or c in "-_")[:24] or "case"
    return "%s-%s" % (tag, row.get("id"))


def case_snapshot(store, case_id: int) -> dict | None:
    """The whole case as the bench stores it, straight from the database."""
    row = store.get(case_id)
    if row is None:
        return None
    row = dict(row)
    atts = []
    for a in store.attempts(case_id):
        a = dict(a)
        atts.append({"n": a.get("n"), "mode": a.get("restore_mode"), "outcome": a.get("outcome"),
                     "stop_stage": a.get("stop_stage"), "stop_step": a.get("stop_step"),
                     "ended": a.get("ended"), "exit_code": a.get("exit_code"),
                     "signatures": json.loads(a.get("signatures") or "[]")})
    verdict = {}
    try:
        verdict = json.loads(row.get("verdict_json") or "{}")
    except Exception:  # noqa: BLE001
        pass
    report_html = ""
    try:
        rp = Path(row.get("folder") or "") / "report.html"
        if rp.is_file() and rp.stat().st_size < 2_000_000:
            report_html = rp.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - the report is a nice-to-have
        pass
    return {"key": case_key(row), "local_id": row.get("id"),
            "created": row.get("created"), "updated": row.get("updated"),
            "device": {"product": row.get("product"), "model_name": row.get("model_name"),
                       "ecid": row.get("ecid"), "serial": row.get("serial")},
            "ticket": row.get("ticket") or "", "notes": row.get("notes") or "",
            "category": row.get("category"), "headline": row.get("headline"),
            "top_part": row.get("top_part"), "confidence": row.get("confidence"),
            "stop_stage": row.get("stop_stage"), "stop_step": row.get("stop_step"),
            "outcome": row.get("outcome"), "fix_part": row.get("fix_part") or "",
            "fix_notes": row.get("fix_notes") or "",
            "attempts": atts, "verdict": verdict, "report_html": report_html}


def post_case(store, case_id: int) -> None:
    snap = case_snapshot(store, case_id)
    if snap is not None:
        _post("/api/restoredoctor/case", snap)


def post_case_async(store, case_id: int | None) -> None:
    if case_id is None or not bench_url():
        return
    threading.Thread(target=post_case, args=(store, case_id), daemon=True).start()


# ------------------------------------------------------------------ live beat
def live_due() -> bool:
    """Cheap pre-check so callers skip building a summary between beats."""
    return bool(bench_url()) and time.monotonic() - _last_live >= LIVE_EVERY_S


def live(state: str, device=None, summary=None, attempt: int | None = None,
         case_id: int | None = None, force: bool = False) -> None:
    """A small throttled beat while a restore runs: stage, step, progress.

    state: restoring | settling | done | failed | idle
    """
    global _last_live
    if not bench_url():
        return
    now = time.monotonic()
    with _lock:
        if not force and now - _last_live < LIVE_EVERY_S:
            return
        _last_live = now
    body = {"state": state, "attempt": attempt, "case_id": case_id}
    if device is not None:
        body["device"] = {"product": getattr(device, "product", None),
                          "model_name": getattr(device, "name", None),
                          "serial": getattr(device, "serial", None)}
    if summary is not None:
        body["stage"] = getattr(summary, "stage", None)
        body["step"] = getattr(summary, "step_plain", None) or getattr(summary, "step", None)
        body["progress_step"] = getattr(summary, "progress_step", None)
        body["progress"] = getattr(summary, "progress", None)
        body["outcome"] = getattr(summary, "outcome", None)
    _spawn("/api/restoredoctor/live", body)
