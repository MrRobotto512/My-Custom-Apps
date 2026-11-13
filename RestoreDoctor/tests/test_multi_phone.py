"""Several phones on one station (0.4): one slot per phone, restores side by
side, each pinned to its own phone, one shared firmware download.

    python -m pytest tests/test_multi_phone.py -q
"""
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("RD_DATA_DIR", tempfile.mkdtemp())

from test_station import _run_until, bench  # noqa: E402,F401  (the scripted bench)


def _station(scenarios="battery_veridian,success_update"):
    from restore_doctor.station import Station
    return Station(sim_id=scenarios)


# ------------------------------------------------------------------ the beat
def test_each_phone_gets_its_own_slot_in_the_beat(bench):
    st = _station()
    st.tick()
    b = bench.beats[-1]
    assert [p["device"]["product"] for p in b["phones"]] == ["iPhone15,4", "iPhone14,5"]
    keys = [p["key"] for p in b["phones"]]
    assert len(set(keys)) == 2 and all(k.startswith("e") for k in keys)
    assert all(p["builds"] and p["state"] == "idle" and p["dfu"]["dfu"] for p in b["phones"])
    assert b["usb"]["count"] == 2 and b["usb"]["multi"] is True
    # a dashboard from before 0.4 still reads the first phone where it always did
    assert b["device"] == b["phones"][0]["device"] and b["state"] == "idle" and b["builds"] == b["phones"][0]["builds"]


def test_two_restores_run_side_by_side_and_end_with_their_own_verdicts(bench):
    st = _station("battery_veridian,success_update")
    st.tick()
    a, b = [p["key"] for p in bench.beats[-1]["phones"]]
    build = bench.beats[-1]["phones"][0]["builds"][0]["build"]
    st.handle({"id": "o-a", "op": "restore", "phone": a, "build": build, "erase": False, "ticket": "A1"})
    st.handle({"id": "o-b", "op": "restore", "phone": b, "build": build, "erase": True, "confirm": "ERASE",
               "ticket": "B2"})
    both = []

    def done():
        both.append({k for k, p in st.phones.items() if p.state == "restoring"})
        return all(p.job.get("phase") in ("done", "failed") for p in st.phones.values())
    assert _run_until(st, done), {k: p.job for k, p in st.phones.items()}
    assert {a, b} in both                                         # really at the same time
    pa, pb = st.phones[a], st.phones[b]
    assert pa.job["phase"] == "failed" and pb.job["phase"] == "done"
    assert pa.job["case_key"] and pb.job["case_key"] and pa.job["case_key"] != pb.job["case_key"]
    assert pa.session.case_id != pb.session.case_id
    assert pa.session.ticket == "A1" and pb.session.ticket == "B2"
    assert pa.job["erase"] is False and pb.job["erase"] is True
    # the logs did not bleed into each other: each attempt folder holds its own engine log
    assert pa.session.attempts[0].folder != pb.session.attempts[0].folder
    st.tick()
    beat = {p["key"]: p for p in bench.beats[-1]["phones"]}
    assert beat[a]["job"]["headline"] and beat[a]["job"]["log"] and beat[b]["job"]["outcome"] == "success"


def test_an_order_for_a_phone_that_is_gone_never_lands_on_another(bench):
    st = _station()
    st.tick()
    build = st.builds[0].build
    st.handle({"id": "o-x", "op": "restore", "phone": "eDEADBEEF", "build": build, "erase": False})
    time.sleep(0.4)
    assert not any(p.session.attempts for p in st.phones.values())
    assert "no longer connected" in st.primary.job["msg"]
    # cancel and New phone are per phone too
    a, b = list(st.phones)
    st.phones[b].job = {"phase": "failed", "msg": "x"}
    st.handle({"id": "o-n", "op": "new_phone", "phone": b})
    assert st.phones[b].job == {} and "no longer connected" in st.phones[a].job["msg"]


def test_same_firmware_is_downloaded_once_for_two_phones(bench, monkeypatch):
    from restore_doctor import station as stn
    from restore_doctor.firmware import Build
    st = _station("success_update,success_update")
    st.tick()
    calls = []

    def slow_download(b, folder, progress, cancelled):
        calls.append(b.build)
        for i in range(6):
            time.sleep(0.1)
            progress(i / 6, "%d of 6" % i)
            assert not cancelled()
        return Path("simulated.ipsw")
    monkeypatch.setattr(stn, "download", slow_download)
    for p in st.phones.values():
        p.builds = [Build("iPhone14,5", "26.0.1", "23A355", True, url="https://example.invalid/fw_23A355.ipsw")]
        p.builds_for = "iPhone14,5"
    a, b = list(st.phones)
    st.handle({"id": "d-a", "op": "restore", "phone": a, "build": "23A355", "erase": False})
    st.handle({"id": "d-b", "op": "restore", "phone": b, "build": "23A355", "erase": False})
    time.sleep(0.3)
    assert {p.state for p in st.phones.values()} == {"downloading"}
    assert "fw_23A355.ipsw" in st._in_use()                       # housekeeping keeps its hands off
    assert _run_until(st, lambda: all(p.job.get("phase") == "done" for p in st.phones.values())), \
        {k: p.job for k, p in st.phones.items()}
    assert calls == ["23A355"]


def test_cancelling_one_waiting_phone_leaves_the_others_download_alone(bench, monkeypatch):
    from restore_doctor import station as stn
    from restore_doctor.firmware import Build
    st = _station("success_update,success_update")
    st.tick()
    stopped = []

    def slow_download(b, folder, progress, cancelled):
        for _ in range(10):
            time.sleep(0.1)
            if cancelled():
                stopped.append(1)
                raise InterruptedError("Download cancelled")
        return Path("simulated.ipsw")
    monkeypatch.setattr(stn, "download", slow_download)
    for p in st.phones.values():
        p.builds = [Build("iPhone14,5", "26.0.1", "23A355", True, url="https://example.invalid/fw_23A355.ipsw")]
        p.builds_for = "iPhone14,5"
    a, b = list(st.phones)
    st.handle({"id": "c-a", "op": "restore", "phone": a, "build": "23A355", "erase": False})   # owns the download
    st.handle({"id": "c-b", "op": "restore", "phone": b, "build": "23A355", "erase": False})
    time.sleep(0.25)
    st.handle({"id": "c-x", "op": "cancel", "phone": a})
    assert _run_until(st, lambda: st.phones[b].job.get("phase") == "done" and st.phones[a].state == "idle")
    assert st.phones[a].job["phase"] == "cancelled" and not st.phones[a].session.attempts
    assert not stopped                                            # phone b still needed the file


# --------------------------------------------------------- telling them apart
REC_A = {"mode": "Recovery", "ecid": 0x1C15D43C20802E, "cpid": 0x8120, "bdid": 0x0E, "serial": "C77Y0VY19C",
         "udid": None, "raw": "CPID:8120 ... ECID:001C15D43C20802E"}
DFU_B = {"mode": "DFU", "ecid": 0xA1B2C3D4E5F26, "cpid": 0x8015, "bdid": 0x0A, "serial": None, "udid": None,
         "raw": "CPID:8015 ... ECID:000A1B2C3D4E5F26"}
NEW_UDID = "00008030-001A2B3C4D5E002E"
OLD_UDID = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def real(monkeypatch):
    """A non-simulated station whose USB list and tools are scripted."""
    from restore_doctor import station as stn
    from restore_doctor.engine import DeviceInfo
    st = stn.Station()
    world = {"usb": [], "udids": [], "asked": [], "info": []}

    class Usb:                      # the passive USB list, scripted
        can_list, available, current, timeline, phone_count = True, False, None, [], 0
        phones = property(lambda self: world["usb"])
        track = untrack = know = lambda self, *a: None
        mode_of = lambda self, e: None
    st.usb = Usb()

    def recovery(ecid=None):
        world["asked"].append(ecid)
        u = next((x for x in world["usb"] if x["ecid"] == ecid), None)
        return DeviceInfo(mode=u["mode"], ecid=ecid, product="iPhone15,5", name="iPhone 15 Plus") if u else None

    def normal(udid):
        world["info"].append(udid)
        new = udid.upper() == NEW_UDID
        return DeviceInfo(mode="Normal", udid=udid, ecid=0x1A2B3C4D5E002E if new else 0x777, ios="18.1",
                          product="iPhone12,1" if new else "iPhone10,4", name="iPhone 11" if new else "iPhone 8")
    monkeypatch.setattr(stn, "probe_recovery", recovery)
    monkeypatch.setattr(stn, "probe_normal", normal)
    monkeypatch.setattr(stn, "list_normal_udids", lambda: list(world["udids"]))
    st.world = world
    return st


def test_two_phones_in_recovery_and_one_in_normal_mode_become_three_slots(real):
    real.world["usb"] = [REC_A, DFU_B]
    real.world["udids"] = [NEW_UDID]
    real._sync(real._enumerate())
    assert list(real.phones) == ["e1C15D43C20802E", "eA1B2C3D4E5F26", "e1A2B3C4D5E002E"]
    assert [p.device.mode for p in real.phones.values()] == ["Recovery", "DFU", "Normal"]
    assert real.world["asked"] == [REC_A["ecid"], DFU_B["ecid"]]       # each asked BY ECID, never "the first one"
    real._sync(real._enumerate())
    assert len(real.world["asked"]) == 2 and real.world["info"] == [NEW_UDID]   # asked once, not every 2 s


def test_nothing_is_asked_of_usb_while_a_restore_runs(real):
    real.world["usb"] = [REC_A]
    real._sync(real._enumerate())
    a = real.phones["e1C15D43C20802E"]
    a.state = "restoring"                                   # phone A is mid-restore
    real.world["asked"].clear()
    # phone A has left Recovery for restore mode. A second phone arrives in
    # DFU, a new-style one in normal mode, and an old-style UDID shows up -
    # which is most likely phone A itself
    real.world["usb"] = [DFU_B]
    real.world["udids"] = [NEW_UDID, OLD_UDID]
    real._sync(real._enumerate())
    assert real.world["asked"] == []                        # irecovery is not run at all
    assert real.world["info"] == [NEW_UDID]                 # provably not phone A: fine to read
    assert OLD_UDID not in real.world["info"]               # might BE phone A: not touched
    b = real.phones["eA1B2C3D4E5F26"]
    assert b.device.mode == "DFU" and b.device.product == "iPhone10,4"   # named from the USB string alone
    assert real.usb_note == ""                              # the unnamed one is accounted for by phone A
    # a SECOND unnamed phone cannot be phone A as well: say one is waiting
    real.world["udids"] = [NEW_UDID, OLD_UDID, "a" * 40]
    real._sync(real._enumerate())
    assert real.usb_note.startswith("1 more phone is plugged in") and len(real.world["info"]) == 1
    real.world["udids"] = [NEW_UDID, OLD_UDID]
    # phone A's own UDID appearing (restore mode) is skipped too
    a.device.udid = "00008120-001C15D43C20802E"
    real.world["udids"] = [a.device.udid]
    real._sync(real._enumerate())
    assert a.device.udid not in real.world["info"] and a.device.mode == "Recovery"
    # once nothing is restoring, the waiting phone is identified
    a.state = "idle"
    real.world["udids"] = [OLD_UDID]
    real._sync(real._enumerate())
    assert OLD_UDID in real.world["info"] and "e777" in real.phones


def test_a_result_stays_after_unplugging_until_new_phone(real, monkeypatch):
    from restore_doctor import station as stn
    monkeypatch.setattr(stn, "GONE_GRACE_S", 0.0)
    real.world["usb"] = [REC_A, DFU_B]
    real._sync(real._enumerate())
    real.phones["e1C15D43C20802E"].job = {"phase": "failed", "headline": "NAND"}
    real.world["usb"] = []
    real._sync(real._enumerate())
    assert list(real.phones) == ["e1C15D43C20802E"]         # the idle one simply went away
    kept = real.phones["e1C15D43C20802E"]
    assert kept.device.connected is False and kept.job["headline"] == "NAND"
    real.handle({"id": "n1", "op": "new_phone", "phone": "e1C15D43C20802E"})
    assert not real.phones


def test_without_a_usable_usb_list_it_falls_back_to_the_one_phone_probe(real):
    type(real.usb).can_list = False
    real.world["usb"] = [REC_A]                             # what irecovery would find
    real.world["udids"] = []
    real._sync(real._enumerate())
    assert real.world["asked"] == [None] and not real.phones   # asked the old way (first phone)


def test_a_restore_is_refused_when_the_phone_cannot_be_pinned(real):
    from restore_doctor.engine import DeviceInfo
    from restore_doctor.firmware import Build
    from restore_doctor.station import Phone
    real.world["usb"] = [REC_A]
    real._sync(real._enumerate())
    anon = real.phones["x0"] = Phone(real, "x0", DeviceInfo(mode="DFU", product="iPhone10,4"))
    anon.builds = [Build("iPhone10,4", "16.7", "20H1", True, path=Path("x.ipsw"))]
    real.checks = []
    real._restore({"id": "r1", "op": "restore", "phone": "x0", "build": "20H1", "erase": False})
    assert "cannot be told apart" in anon.job["msg"] and not anon.session.attempts


def test_the_engine_is_pinned_to_one_phone():
    from restore_doctor.engine import RestoreJob
    j = RestoreJob(Path("a.ipsw"), False, 0x1C15D43C20802E, Path("."), None, None)
    assert j.command()[-3:-1] == ["-i", "0x1c15d43c20802e"]
    j = RestoreJob(Path("a.ipsw"), True, None, Path("."), None, None, udid=OLD_UDID)
    assert j.command()[-3:-1] == ["-u", OLD_UDID]


# ------------------------------------------------------------------ usbwatch
def test_usb_ids_say_which_phone_is_which():
    from restore_doctor import usbwatch as u
    ids = [r"USB\VID_05AC&PID_1281\CPID:8120_CPRV:11_CPFM:03_SCEP:01_BDID:0E_ECID:001C15D43C20802E_IBFL:3D_SRNM:[C77Y0VY19C]",
           r"USB\VID_05AC&PID_12A8\00008030001A2B3C4D5E002E",
           r"USB\VID_05AC&PID_12A8&MI_00\6&1A2B&0&0000",
           r"USB\VID_05AC&PID_12A8\0123456789ABCDEF0123456789ABCDEF01234567",
           r"USB\VID_05AC&PID_024F\KEYBOARD"]
    got = u.phones_from_ids(ids)
    assert [(p["mode"], p["ecid"], p["udid"]) for p in got] == [
        ("Recovery", 0x1C15D43C20802E, None), ("Normal", 0x1A2B3C4D5E002E, NEW_UDID), ("Normal", None, OLD_UDID)]
    assert got[0]["serial"] == "C77Y0VY19C" and got[0]["cpid"] == 0x8120 and got[0]["bdid"] == 0x0E
    assert u.phone_modes(ids) == ["Recovery", "Normal", "Normal"]      # the old view is unchanged


def test_each_phone_keeps_its_own_usb_timeline():
    from restore_doctor.usbwatch import PhoneUsb, UsbWatch
    w = UsbWatch()
    w.available = True
    A, B = REC_A["ecid"], DFU_B["ecid"]
    w.track(A), w.track(B)
    pa, pb = PhoneUsb(w, lambda: A), PhoneUsb(w, lambda: B)
    w._report_phones([REC_A, DFU_B])
    t0 = time.monotonic()
    w._report_phones([DFU_B])                                  # A dropped off the bus
    w._report_phones([DFU_B, {"mode": "Normal", "ecid": None, "udid": OLD_UDID, "raw": ""}])   # ...and came back unnamed
    assert [m for _, m in pa.since(t0)] == ["Recovery", "Gone", "Normal"]      # one missing, one unnamed: it is A
    assert [m for _, m in pb.since(t0)] == ["DFU"]
    assert pa.available and pb.available and pa.current == "Normal"
    # two missing and one unnamed: cannot say whose it is - no USB evidence rather than the wrong phone's
    pa.t0 = pb.t0 = time.monotonic()
    w._udid_ecid.clear()
    w._report_phones([{"mode": "Normal", "ecid": None, "udid": "f" * 40, "raw": ""}])
    assert pa.current == "?" and not pa.available and not pb.available
