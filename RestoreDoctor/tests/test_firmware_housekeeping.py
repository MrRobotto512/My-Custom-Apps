"""Firmware housekeeping: the inventory, safe delete, and the keep-clean policies.

    python -m pytest tests/test_firmware_housekeeping.py -q
"""
import os
import plistlib
import tempfile
import time
import zipfile
from pathlib import Path

os.environ.setdefault("RD_DATA_DIR", tempfile.mkdtemp())

from restore_doctor import firmware as fw  # noqa: E402


def _ipsw(folder: Path, name: str, version: str, build: str, products=("iPhone12,1",), age_s=0) -> Path:
    p = folder / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("BuildManifest.plist", plistlib.dumps(
            {"SupportedProductTypes": list(products), "ProductVersion": version,
             "ProductBuildVersion": build}))
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


def test_inventory_reads_versions_and_flags_partials():
    d = Path(tempfile.mkdtemp())
    _ipsw(d, "a.ipsw", "18.6", "22G86")
    (d / "b.ipsw.part").write_bytes(b"x" * 10)
    (d / "notes.txt").write_text("not firmware")
    inv = {f["name"]: f for f in fw.inventory(d)}
    assert set(inv) == {"a.ipsw", "b.ipsw.part"}
    assert inv["a.ipsw"]["version"] == "18.6" and inv["a.ipsw"]["build"] == "22G86"
    assert inv["b.ipsw.part"]["part"] is True


def test_delete_is_by_name_inside_the_folder_only():
    d = Path(tempfile.mkdtemp())
    _ipsw(d, "a.ipsw", "18.6", "22G86")
    outside = Path(tempfile.mkdtemp()) / "keep.ipsw"
    outside.write_bytes(b"x")
    assert fw.delete_file(d, "../" + outside.name) is False
    assert fw.delete_file(d, str(outside)) is False
    assert fw.delete_file(d, "settings.json") is False          # not firmware
    assert outside.exists()
    assert fw.delete_file(d, "a.ipsw") is True and not (d / "a.ipsw").exists()


def test_keep_newest_keeps_one_per_model_family():
    d = Path(tempfile.mkdtemp())
    _ipsw(d, "11_old.ipsw", "18.5", "22F76")
    _ipsw(d, "11_new.ipsw", "18.10", "22K10")                   # 18.10 > 18.5: numeric, not text
    _ipsw(d, "13_only.ipsw", "18.6", "22G86", products=("iPhone14,5",))
    (d / "broken.ipsw").write_bytes(b"not a zip")               # unreadable: left for a human
    gone = fw.cleanup(d, "keep_newest")
    assert gone == ["11_old.ipsw"]
    assert sorted(p.name for p in d.iterdir()) == ["11_new.ipsw", "13_only.ipsw", "broken.ipsw"]


def test_keep_newest_never_removes_the_file_in_use():
    d = Path(tempfile.mkdtemp())
    _ipsw(d, "old.ipsw", "18.5", "22F76")
    _ipsw(d, "new.ipsw", "18.6", "22G86")
    assert fw.cleanup(d, "keep_newest", keep=("old.ipsw",)) == []
    assert (d / "old.ipsw").exists()


def test_delete_after_restore_and_keep_all():
    d = Path(tempfile.mkdtemp())
    _ipsw(d, "used.ipsw", "18.6", "22G86")
    _ipsw(d, "other.ipsw", "18.5", "22F76")
    assert fw.cleanup(d, "keep_all", used="used.ipsw") == []
    assert fw.cleanup(d, "delete_after_restore", used="used.ipsw") == ["used.ipsw"]
    assert (d / "other.ipsw").exists()


def test_abandoned_partial_downloads_are_swept_under_every_policy():
    d = Path(tempfile.mkdtemp())
    stale = d / "old.ipsw.part"
    stale.write_bytes(b"x")
    t = time.time() - 8 * 86400
    os.utime(stale, (t, t))
    (d / "fresh.ipsw.part").write_bytes(b"x")
    assert fw.cleanup(d, "keep_all") == ["old.ipsw.part"]
    assert (d / "fresh.ipsw.part").exists()
