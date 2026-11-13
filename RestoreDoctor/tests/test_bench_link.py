"""bench.py: Restore Doctor reporting into the Hero Dashboard.

A tiny local HTTP server plays the bench; a real CaseStore in a temp folder
plays the shop PC. Offline (no bench at all) must change nothing.

    python -m pytest tests/test_bench_link.py -q
"""
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

os.environ["RD_DATA_DIR"] = tempfile.mkdtemp()


class _Bench(HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.posts = []          # (path, body-dict)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.posts.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *a):   # noqa: D102 - quiet
        pass


@pytest.fixture
def bench(monkeypatch):
    srv = _Bench()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    from restore_doctor import bench as b
    monkeypatch.setenv("RD_BENCH", "http://127.0.0.1:%d" % srv.server_port)
    b._url_cache = None          # the cache is per-process; tests change the env
    b._last_live = 0.0
    yield srv
    srv.shutdown()
    b._url_cache = None


class _FakeVerdict:
    category = "hardware"
    headline = "Battery gas gauge is the lead suspect"
    confidence = "high"
    stop = {"stage": "restore_os", "step": "Updating gas gauge software"}
    suspects = []

    def as_dict(self):
        return {"category": self.category, "headline": self.headline,
                "confidence": self.confidence, "stop": self.stop,
                "suspects": [{"part": "battery_bms", "label": "Battery / gas gauge",
                              "score": 0.82, "reasons": ["died updating gas gauge"]}]}


def _case(store):
    cid, folder = store.new_case("iPhone12,1", "iPhone 11", 0x1A2B, "F2LXK1", ticket="4821")
    (Path(folder) / "report.html").write_text("<!doctype html><html><body>rep</body></html>",
                                              encoding="utf-8")
    v = _FakeVerdict()
    v.suspects = []
    store.set_verdict(cid, v, None)
    return cid


def _wait_posts(srv, n, s=5.0):
    deadline = time.time() + s
    while time.time() < deadline and len(srv.posts) < n:
        time.sleep(0.05)
    return srv.posts


def test_post_case_carries_the_whole_story(bench):
    from restore_doctor import bench as b
    from restore_doctor.cases import CaseStore
    store = CaseStore(Path(tempfile.mkdtemp()) / "cases.db")
    cid = _case(store)
    b.post_case(store, cid)
    path, body = bench.posts[-1]
    assert path == "/api/restoredoctor/case"
    assert body["key"] == "F2LXK1-%d" % cid and body["ticket"] == "4821"
    assert body["device"]["model_name"] == "iPhone 11" and body["device"]["serial"] == "F2LXK1"
    assert body["headline"].startswith("Battery gas gauge")
    assert body["verdict"]["suspects"][0]["label"] == "Battery / gas gauge"
    assert "rep" in body["report_html"]


def test_record_fix_reposts_the_case(bench):
    from restore_doctor.cases import CaseStore
    store = CaseStore(Path(tempfile.mkdtemp()) / "cases.db")
    cid = _case(store)
    store.record_fix(cid, "Battery (BMS)", "swap fixed it")
    posts = _wait_posts(bench, 1)
    assert posts, "record_fix posted nothing"
    body = posts[-1][1]
    assert body["fix_part"] == "Battery (BMS)" and body["fix_notes"] == "swap fixed it"


def test_live_beat_is_small_and_throttled(bench):
    from restore_doctor import bench as b

    class S:  # the few AttemptSummary fields the beat reads
        stage = "restore_os"
        step_plain = "Flashing NOR"
        step = "flash_nor"
        progress_step = 4
        progress = 0.62
        outcome = "running"

    class D:
        product = "iPhone12,1"
        name = "iPhone 11"
        serial = "F2LXK1"

    b.live("restoring", D(), S(), attempt=1, case_id=3, force=True)
    posts = _wait_posts(bench, 1)
    assert posts[-1][0] == "/api/restoredoctor/live"
    body = posts[-1][1]
    assert body["state"] == "restoring" and body["step"] == "Flashing NOR"
    assert body["progress"] == 0.62 and body["device"]["model_name"] == "iPhone 11"
    # straight after a beat, the next one is not yet due
    assert not b.live_due()
    n = len(bench.posts)
    b.live("restoring", D(), S(), attempt=1, case_id=3)    # throttled: nothing sent
    time.sleep(0.3)
    assert len(bench.posts) == n


def test_no_bench_is_silent(monkeypatch):
    from restore_doctor import bench as b
    from restore_doctor.cases import CaseStore
    monkeypatch.setenv("RD_BENCH", "off")
    b._url_cache = None
    assert b.bench_url() == ""
    store = CaseStore(Path(tempfile.mkdtemp()) / "cases.db")
    cid = _case(store)
    b.post_case(store, cid)                                 # must not raise
    store.record_fix(cid, "x", "")                          # must not raise
    assert not b.live_due()
    b._url_cache = None
