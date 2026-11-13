"""station.py: Restore Doctor driven from the Hero Dashboard.

A scripted local HTTP server plays the bench (it hands out orders in its
replies, exactly as the dashboard does); the station runs the app's own
simulation scenarios, so the engine parser, the verdict and the case store
are all the real ones.

    python -m pytest tests/test_station.py -q
"""
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

os.environ.setdefault("RD_DATA_DIR", tempfile.mkdtemp())


class _Bench(HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.beats = []           # station check-ins
        self.posts = []           # (path, body) for everything else
        self.order = None         # handed out until acked


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv = self.server
        reply = {"ok": True}
        if self.path == "/api/restoredoctor/station":
            srv.beats.append(body)
            if srv.order and body.get("ack") == srv.order["id"]:
                srv.order = None
            reply["cmd"] = srv.order
        else:
            srv.posts.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(reply).encode())

    def log_message(self, *a):   # noqa: D102
        pass


@pytest.fixture
def bench(monkeypatch):
    srv = _Bench()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from restore_doctor import bench as b
    monkeypatch.setenv("RD_BENCH", "http://127.0.0.1:%d" % srv.server_port)
    b._url_cache = None
    yield srv
    srv.shutdown()
    b._url_cache = None


def _station(scenario="battery_veridian"):
    from restore_doctor.station import Station
    return Station(sim_id=scenario)


def _run_until(st, pred, s=40.0):
    deadline = time.time() + s
    while time.time() < deadline:
        st.tick()
        if pred():
            return True
        time.sleep(0.15)
    return False


def test_beat_reports_phone_firmware_and_identity(bench):
    st = _station()
    st.tick()
    b = bench.beats[-1]
    assert b["station"] == st.sid and b["state"] == "idle" and b["sim"] is True
    assert b["device"]["connected"] is True and b["device"]["product"]
    assert b["builds"] and b["builds"][0]["on_disk"] is True
    # the id survives a restart
    assert _station().sid == st.sid


def test_update_order_runs_to_a_verdict_and_posts_the_case(bench):
    st = _station("battery_veridian")
    st.tick()
    build = bench.beats[-1]["builds"][0]["build"]
    bench.order = {"id": "ord-1", "op": "restore", "build": build, "erase": False,
                   "confirm": "", "ticket": "4821"}
    assert _run_until(st, lambda: st.job.get("phase") in ("done", "failed")), st.job
    assert bench.order is None                                    # the station acked it
    assert st.job["phase"] == "failed" and st.job["headline"]
    assert st.job["case_key"] and st.state == "idle"
    states = {b["state"] for b in bench.beats}
    assert "restoring" in states                                  # the dashboard saw it run
    mid = [b["job"] for b in bench.beats if b["state"] == "restoring" and b["job"].get("stage")]
    assert mid, "no live stage reached the bench"
    # the finished case went up with the ticket the tech typed on the dashboard
    deadline = time.time() + 5
    while time.time() < deadline and not any(p == "/api/restoredoctor/case" for p, _ in bench.posts):
        time.sleep(0.1)
    cases = [body for p, body in bench.posts if p == "/api/restoredoctor/case"]
    assert cases and cases[-1]["ticket"] == "4821" and cases[-1]["key"] == st.job["case_key"]


def test_erase_without_the_typed_word_never_starts(bench):
    st = _station()
    st.tick()
    build = bench.beats[-1]["builds"][0]["build"]
    st.handle({"id": "ord-2", "op": "restore", "build": build, "erase": True, "confirm": ""})
    time.sleep(0.5)
    assert st.job["phase"] == "failed" and "ERASE" in st.job["msg"]
    assert not st.session.attempts and st.state == "idle"


def test_update_refuses_to_downgrade_and_unknown_builds(bench):
    st = _station()
    st.tick()
    build = st.builds[0]
    st.session.device.ios = "99.0"
    st.handle({"id": "ord-3", "op": "restore", "build": build.build, "erase": False})
    time.sleep(0.5)
    assert st.job["phase"] == "failed" and "cannot downgrade" in st.job["msg"]
    st.handle({"id": "ord-4", "op": "restore", "build": "NOPE1", "erase": False})
    time.sleep(0.5)
    assert st.job["phase"] == "failed" and "no longer in this station" in st.job["msg"]
    assert not st.session.attempts


def test_an_order_is_handled_once_however_often_it_is_resent(bench):
    st = _station()
    st.tick()
    calls = []
    st._restore = lambda cmd: calls.append(cmd["id"])
    cmd = {"id": "ord-5", "op": "restore", "build": st.builds[0].build, "erase": False}
    st.handle(cmd)
    st.handle(cmd)
    time.sleep(0.3)
    assert calls == ["ord-5"]


def test_firmware_is_reported_and_managed_by_order(bench, tmp_path):
    import plistlib
    import zipfile
    from restore_doctor.station import Station

    def ipsw(name, version, build):
        with zipfile.ZipFile(tmp_path / name, "w") as z:
            z.writestr("BuildManifest.plist", plistlib.dumps(
                {"SupportedProductTypes": ["iPhone12,1"], "ProductVersion": version,
                 "ProductBuildVersion": build}))

    ipsw("old.ipsw", "18.5", "22F76")
    ipsw("new.ipsw", "18.6", "22G86")
    st = Station(sim_id="battery_veridian", ipsw_folder=tmp_path)
    st.policy = "keep_all"
    st.tick()
    f = bench.beats[-1]["firmware"]
    assert f["folder"] == str(tmp_path) and f["policy"] == "keep_all"
    assert {x["name"] for x in f["files"]} == {"old.ipsw", "new.ipsw"} and f["total"] > 0
    # delete one by order
    st.handle({"id": "fw-1", "op": "fw_delete", "name": "old.ipsw"})
    assert not (tmp_path / "old.ipsw").exists() and "Deleted old.ipsw" in st.fw_note
    # a path is never accepted
    st.handle({"id": "fw-2", "op": "fw_delete", "name": "../new.ipsw"})
    assert (tmp_path / "new.ipsw").exists()
    # switching policy applies it and is remembered
    ipsw("older.ipsw", "18.1", "22B83")
    st.handle({"id": "fw-3", "op": "fw_policy", "policy": "keep_newest"})
    deadline = time.time() + 5
    while time.time() < deadline and (tmp_path / "older.ipsw").exists():
        time.sleep(0.05)
    assert not (tmp_path / "older.ipsw").exists() and (tmp_path / "new.ipsw").exists()
    assert Station._load_policy() == "keep_newest"
    st.handle({"id": "fw-4", "op": "fw_policy", "policy": "nonsense"})
    assert st.policy == "keep_newest"


def test_store_apple_devices_without_a_driver_yet_is_not_an_error(monkeypatch):
    """Apple Devices (Store) installed, no iPhone ever plugged in: the driver
    package is not there yet - a hint, not a red check telling the tech to
    install the app they already have."""
    import subprocess
    from restore_doctor import engine

    class P:
        stdout = "Published Name: oem1.inf\nProvider Name: Intel\n"

    class Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(engine.paths, "IS_WINDOWS", True)
    monkeypatch.setattr(engine, "NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    monkeypatch.setattr(engine.socket, "create_connection", lambda *a, **k: Sock())
    r = engine.check_driver()
    assert r.ok is None and "first time an iPhone is plugged in" in r.detail and "Plug an iPhone in" in r.fix

    def refuse(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr(engine.socket, "create_connection", refuse)
    assert engine.check_driver().ok is False               # no Apple software at all: still red
    P.stdout = "Provider Name:      Apple, Inc.\n"
    assert engine.check_driver().ok is True


def test_beat_carries_what_the_dashboards_app_layout_shows(bench):
    """Stages, signals, USB timeline, engine log, the DFU helper and the parts
    list - the panels of the desktop app's Restore tab, for the dashboard."""
    st = _station("battery_veridian")
    st.tick()
    b = bench.beats[-1]
    assert b["dfu"]["dfu"] and b["dfu"]["recovery"] and b["usb"]["mode"]
    assert b["parts"] and all(len(p) == 2 for p in b["parts"])
    build = b["builds"][0]["build"]
    bench.order = {"id": "ord-9", "op": "restore", "build": build, "erase": False, "notes": "from the dashboard"}
    assert _run_until(st, lambda: st.job.get("phase") in ("done", "failed")), st.job
    st.tick()
    j = bench.beats[-1]["job"]
    assert j["attempt"] == 1 and j["stage"] and j["max_stage"] and j["log"] and j["ipsw"]
    assert isinstance(j["signals"], list) and isinstance(j["usb_tl"], list)
    assert st.session.notes == "from the dashboard"
    key = st.job["case_key"]
    # record the fix by order: only this station's own case, matched by its key
    part = b["parts"][0][0]
    st.handle({"id": "fix-bad", "op": "record_fix", "key": "NOPE-999", "part": part})
    st.handle({"id": "fix-1", "op": "record_fix", "key": key, "part": part, "notes": "swap"})
    row = dict(st.store.get(st.session.case_id))
    assert row["fix_part"] == part and row["fix_notes"] == "swap"
    # code lookup answers in the beat
    st.handle({"id": "lk-1", "op": "lookup", "code": "abc"})
    assert st.lookup["found"] is False
    st.handle({"id": "lk-2", "op": "lookup", "code": "4013"})
    st.tick()
    assert bench.beats[-1]["lookup"]["code"] == 4013
    # New phone clears the job for the next one
    st.handle({"id": "np-1", "op": "new_phone"})
    assert st.job == {} and st.session.case_id is None
