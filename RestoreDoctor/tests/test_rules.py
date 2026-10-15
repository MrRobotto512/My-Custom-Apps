"""Regression tests: every scenario log must parse and land on the expected verdict.

Run:  python -m pytest -q tests
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from restore_doctor.diagnose import diagnose, observe_usb  # noqa: E402
from restore_doctor.kb import get_kb  # noqa: E402
from restore_doctor.logparse import RestoreLogParser  # noqa: E402
from restore_doctor.models import find_by_product, usb_chip_name, has_face_id, dfu_steps  # noqa: E402
from restore_doctor.panic import read_panic  # noqa: E402

SC_DIR = ROOT / "restore_doctor" / "scenarios"
INDEX = {s["id"]: s for s in json.loads((SC_DIR / "scenarios.json").read_text())["scenarios"]}


def run(sid):
    sc = INDEX[sid]
    p = RestoreLogParser(clock=lambda: 0.0)
    for line in (SC_DIR / sc["log"]).read_text().splitlines():
        p.feed(line, "out" if line.startswith("progress:") else "err")
    a = p.finish(sc["exit_code"])
    usb = observe_usb([tuple(x) for x in sc["usb"]], a)
    return a, usb, find_by_product(sc["product"]) if sc["product"] else None


# scenario -> (category, top suspect, confidence)
EXPECTED = {
    "success_update": ("success", None, "high"),
    "nand_disk_failure": ("hardware", "nand", "high"),
    "baseband_1015": ("hardware", "baseband", "high"),
    "baseband_no_snum": ("hardware", "baseband", None),
    "faceid_savage": ("hardware", "faceid", None),
    "restore_os_vanished": ("inconclusive", "nand", "low"),
    "restore_os_pc_side": ("setup", "pc_drivers", None),
    "write_drop": ("inconclusive", "nand", "low"),
    "write_drop_repeat": ("hardware", "nand", None),
    "not_signed": ("firmware", "firmware_signing", "high"),
    "no_device": ("setup", "usb_link", None),
    "dfu_hop_drop": ("inconclusive", "usb_link", "low"),
    "battery_veridian": ("inconclusive", "battery", "low"),
}


@pytest.mark.parametrize("sid", sorted(EXPECTED))
def test_scenario_verdict(sid):
    a, usb, model = run(sid)
    v = diagnose([a], model, usb=[usb])
    cat, top, conf = EXPECTED[sid]
    assert v.category == cat, v.headline
    if top:
        assert v.suspects and v.suspects[0].part == top, [(s.part, s.score) for s in v.suspects]
    if conf:
        assert v.confidence == conf
    assert v.headline


def test_every_scenario_is_listed():
    assert set(EXPECTED) == set(INDEX)


def test_repeat_raises_confidence():
    a1, u1, m = run("write_drop")
    a2, u2, _ = run("write_drop_repeat")
    single = diagnose([a1], m, usb=[u1])
    both = diagnose([a1, a2], m, usb=[u1, u2])
    assert single.category == "inconclusive"
    assert both.category == "hardware" and both.repeated and both.confidence == "high"
    assert both.suspects[0].part == "nand"
    assert not any("once more" in c for c in both.next_checks)


def test_moving_stop_points_to_connection():
    a1, u1, m = run("write_drop")
    a2, u2, _ = run("battery_veridian")
    v = diagnose([a1, a2], m, usb=[u1, u2])
    assert not v.repeated
    assert any(s.part == "usb_link" and "moved" in " ".join(s.reasons) for s in v.suspects)


def test_stop_points():
    a, _, _ = run("baseband_1015")
    assert a.stop_stage == "firmware" and a.stop_step == "Updating baseband" and a.stop_part == "baseband"
    assert a.device["radio_error"] == "1"
    a, _, _ = run("write_drop")
    assert a.stop_stage == "storage" and a.stop_progress == pytest.approx(0.4)
    a, _, _ = run("no_device")
    assert a.stop_stage == "detect"
    a, _, _ = run("not_signed")
    assert a.stop_stage == "sign"


def test_device_identity_and_logs():
    a, _, _ = run("nand_disk_failure")
    assert a.device["product"] == "iPhone12,1" and a.device["ecid"] == 7298405312345678
    assert a.restore_kind == "erase"
    assert any("ANS2" in ln for ln in a.device_log)
    a, _, _ = run("write_drop_repeat")
    assert a.panic_log and "nand_read" in " ".join(a.panic_log)


def test_context_and_bench_inputs_shift_scores():
    a, u, m = run("restore_os_vanished")
    base = diagnose([a], m, usb=[u])
    liquid = diagnose([a], m, usb=[u], context={"liquid"})
    def score(v, part):
        return next((s.score for s in v.suspects if s.part == part), 0)
    assert score(liquid, "i2c_peripheral") > score(base, "i2c_peripheral")
    cycling = diagnose([a], m, usb=[u], dc_reading="nand_rail_cycle")
    assert cycling.suspects[0].part == "nand"


def test_success_ignores_setup_noise():
    a, u, m = run("success_update")
    v = diagnose([a], m, usb=[u])
    assert v.category == "success" and not v.suspects


def test_panic_reader():
    p = read_panic(["panic(cpu 1 caller 0xfffffff0): userspace watchdog timeout: no successful checkins "
                    "from thermalmonitord", "Missing sensor(s): TG0B"])
    assert p.watchdog_daemon == "thermalmonitord"
    assert p.missing_sensors == ["TG0B"]
    assert any(part == "battery" for part, _, _ in p.findings)


def test_models():
    assert usb_chip_name(find_by_product("iPhone9,1")) == "Tristar"
    assert usb_chip_name(find_by_product("iPhone12,1")) == "Hydra"
    assert usb_chip_name(find_by_product("iPhone16,1")) == "the USB-C port controller"
    assert has_face_id(find_by_product("iPhone10,3")) and not has_face_id(find_by_product("iPhone10,4"))
    assert "Volume Down" in " ".join(dfu_steps(find_by_product("iPhone9,2")))
    assert "Home" in " ".join(dfu_steps(find_by_product("iPhone8,1")))
    assert find_by_product("iPhone18,1").name == "iPhone 17 Pro"


def test_kb_consistency():
    kb = get_kb()
    parts = set(kb.components)
    for op in kb.operations.values():
        assert op["part"] is None or op["part"] in parts, op
    for sig in kb.signatures:
        for part, _ in sig.parts:
            assert part in parts, sig.id
    for act in kb.activities:
        assert act.part is None or act.part in parts
    for e in kb.apple_codes:
        for p in e["parts"]:
            assert p["part"] in parts
    assert kb.lookup_apple_code(4013)["kind"] == "hardware"
    assert kb.lookup_apple_code(2005)["kind"] == "setup"
    assert kb.lookup_apple_code(3194)["kind"] == "setup"


def test_stdout_only_import():
    """A log captured from stdout only (no timestamps) still parses."""
    p = RestoreLogParser(clock=lambda: 0.0)
    for line in ["Found device in Recovery mode", "Identified device as n104ap, iPhone12,1",
                 "About to restore device... ", "Waiting for NAND (28)", "Got status message",
                 "Status: Disk Failure", "RESTORE FAILED"]:
        p.feed(line, "out")
    a = p.finish(1)
    assert a.outcome == "failed" and 6 in a.status_codes and a.stop_part == "nand"
