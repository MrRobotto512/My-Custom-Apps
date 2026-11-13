"""Restore STATION: Restore Doctor driven from the shop's Hero Dashboard.

    RestoreDoctor.exe --station [--bench=http://benchtool.local:8377] [--headless]

A station is any computer with the restore engine and a USB port. It checks
in with the bench every couple of seconds — its ready checks, the phone on
the cable, the firmware Apple signs for it — and the bench answers with the
tech's order from the dashboard: Update, Erase, Cancel. The restore itself
is the very same Session the desktop app runs (same engine, same parser,
same verdict, same case store), so a restore started from a browser leaves
exactly the case a restore started at the PC does, and bench.py reports it.

The protocol is one POST (see beat()) and platform-neutral on purpose: a
Windows PC is a station today; the bench Pi can be one tomorrow with the
Linux engine build and `--headless`.

Safety, mirrored from the desktop UI's dialogs because no one is standing at
this PC to click them:
  * an Erase order must carry the typed word ERASE (the dashboard collects
    it, the server checks it, and the station checks it again here);
  * an Update never runs onto a phone whose iOS is newer than the build
    (it cannot downgrade and may lose data) unless the order says force;
  * while a restore runs the station does not probe USB — two tools talking
    to one phone can break a restore.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from . import __version__, bench, paths
from .cases import CaseStore
from .engine import run_ready_checks
from . import firmware as fw
from .firmware import StaleList, download, local_builds, merge, signed_builds
from .session import Session

BEAT_S = 2.0
CHECKS_EVERY_S = 60.0
CRITICAL_CHECKS = ("engine", "amds")     # without these a restore cannot even start


def station_id() -> tuple[str, str]:
    """(stable id, display name) — the id survives restarts in station.json."""
    name = socket.gethostname() or "station"
    p = paths.data_dir() / "station.json"
    try:
        sid = json.loads(p.read_text(encoding="utf-8"))["id"]
    except Exception:  # noqa: BLE001
        sid = "%s-%s" % (re.sub(r"[^A-Za-z0-9]", "", name)[:16] or "station", uuid.uuid4().hex[:6])
        try:
            p.write_text(json.dumps({"id": sid}), encoding="utf-8")
        except OSError:
            pass
    return sid, name


def _ver_tuple(v: str) -> list[int]:
    parts = [int(x) for x in re.findall(r"\d+", v or "")][:4]
    return parts + [0] * (4 - len(parts))


class Station:
    def __init__(self, sim_id: str | None = None, ipsw_folder: Path | None = None):
        self.sim_id = sim_id
        self.sid, self.name = station_id()
        self.store = CaseStore()
        if sim_id:
            from .sim import SimUsbWatch
            self.usb = SimUsbWatch(on_change=None)
        else:
            from .usbwatch import UsbWatch
            self.usb = UsbWatch(on_change=None)
        self.session = Session(self.store, self._notify, usb_watch=self.usb, sim_id=sim_id)
        self.ipsw_folder = Path(ipsw_folder) if ipsw_folder else self._ipsw_folder()
        self.checks: list = []
        self._checks_t = 0.0
        self.builds: list = []
        self.builds_note = ""
        self._builds_for: str | None = None
        self.state = "idle"                  # idle | downloading | restoring
        self.job: dict = {}                  # the current / last order's progress
        self._ack: str | None = None
        self._cancel_dl = False
        self.policy = self._load_policy()
        self._inv: list = []
        self._inv_t = 0.0
        self.fw_note = ""                    # what housekeeping last did, for the dashboard
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status_line = "starting"        # for the little window / console

    @staticmethod
    def _ipsw_folder() -> Path:
        try:   # honour the desktop app's "Choose IPSW folder" setting
            s = json.loads((paths.data_dir() / "settings.json").read_text(encoding="utf-8"))
            if s.get("ipsw_folder"):
                return Path(s["ipsw_folder"])
        except Exception:  # noqa: BLE001
            pass
        return paths.ipsw_dir()

    # --------------------------------------------------- firmware folder
    @staticmethod
    def _load_policy() -> str:
        try:
            pol = json.loads((paths.data_dir() / "settings.json").read_text(encoding="utf-8")).get("ipsw_policy")
        except Exception:  # noqa: BLE001
            pol = None
        return pol if pol in fw.POLICIES else fw.DEFAULT_POLICY

    def _save_policy(self, pol: str) -> None:
        p = paths.data_dir() / "settings.json"
        try:
            cur = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cur = {}
        cur["ipsw_policy"] = pol
        try:
            p.write_text(json.dumps(cur, indent=1), encoding="utf-8")
        except OSError:
            pass

    def _inventory(self, force: bool = False) -> list:
        if force or time.monotonic() - self._inv_t > 15 or not self._inv_t:
            try:
                self._inv = fw.inventory(self.ipsw_folder)
            except Exception:  # noqa: BLE001
                self._inv = []
            self._inv_t = time.monotonic()
        return self._inv

    def _in_use(self) -> tuple:
        """Firmware a download or restore is using right now - never deleted."""
        names = []
        if self.state != "idle":
            if self.session.running and self.session.attempts:
                names.append(Path(str(self.session.attempts[-1].ipsw)).name)
            b = self.job.get("file")
            if b:
                names += [b, b + ".part"]
        return tuple(names)

    def _housekeep(self, used: str | None = None) -> None:
        try:
            gone = fw.cleanup(self.ipsw_folder, self.policy, keep=self._in_use(), used=used)
        except Exception:  # noqa: BLE001
            gone = []
        if gone:
            self.fw_note = "Removed %s" % ", ".join(gone[:4]) + (" ..." if len(gone) > 4 else "")
            self._builds_for = None               # on-disk flags changed: re-list
        self._inventory(force=True)

    def open_folder(self) -> None:
        """Show the firmware folder on THIS computer's screen."""
        folder = str(self.ipsw_folder)
        try:
            self.ipsw_folder.mkdir(parents=True, exist_ok=True)
            if paths.IS_WINDOWS:
                os.startfile(folder)              # noqa: S606 - a folder, in Explorer
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:  # noqa: BLE001 - headless station: nothing to open on
            pass

    # ------------------------------------------------------------ session
    def _notify(self, kind: str, payload: dict) -> None:
        if kind == "attempt_finished":
            s, v = payload.get("summary"), payload.get("verdict")
            ok = getattr(s, "outcome", "") == "success"
            row = self.store.get(self.session.case_id) if self.session.case_id else None
            with self._lock:
                self.job.update({"phase": "done" if ok else "failed", "pct": 1.0 if ok else self.job.get("pct"),
                                 "outcome": getattr(s, "outcome", ""),
                                 "headline": getattr(v, "headline", "") or "",
                                 "case_key": bench.case_key(dict(row)) if row else ""})
                self.state = "idle"
            used = Path(str(self.session.attempts[-1].ipsw)).name if (ok and self.session.attempts) else None
            threading.Thread(target=self._housekeep, args=(used,), daemon=True).start()
        elif kind == "attempt_aborted":
            with self._lock:
                self.job.update({"phase": "failed", "msg": str(payload.get("error", ""))[:300]})
                self.state = "idle"

    # -------------------------------------------------------------- facts
    def _refresh_checks(self, force: bool = False) -> None:
        if force or time.monotonic() - self._checks_t > CHECKS_EVERY_S or not self.checks:
            if self.sim_id:
                self.checks = []
            else:
                try:
                    self.checks = run_ready_checks(self.ipsw_folder)
                except Exception:  # noqa: BLE001 - a probe must never stop the loop
                    pass
            self._checks_t = time.monotonic()

    def _refresh_builds(self, force: bool = False) -> None:
        product = self.session.device.product
        if not product:
            self.builds, self.builds_note, self._builds_for = [], "", None
            return
        if not force and product == self._builds_for:
            return
        local = local_builds(self.ipsw_folder, product)
        note = ""
        if self.sim_id:
            from .firmware import Build
            got = merge([Build(product, "26.0.1", "23A355", True, url=None, path=Path("simulated.ipsw"))], local)
        else:
            try:
                got = merge(signed_builds(product), local)
            except StaleList as stale:
                got, note = merge(stale.builds, local), str(stale)
            except Exception as exc:  # noqa: BLE001
                got, note = local, "Could not reach api.ipsw.me (%s); showing local files only." % exc
        self.builds, self.builds_note, self._builds_for = got, note, product

    def _body(self) -> dict:
        d = self.session.device
        with self._lock:
            job = dict(self.job)
            state = self.state
        if state == "restoring" and self.session.attempts:
            s = self.session.attempts[-1].parser.summary()
            job.update({"stage": s.stage, "step": s.step_plain or s.step,
                        "pct": s.progress, "progress_step": s.progress_step})
        return {
            "station": self.sid, "name": self.name, "version": __version__,
            "platform": platform.system().lower(), "sim": bool(self.sim_id),
            "state": state, "ack": self._ack,
            "checks": [dict(c.as_dict(), key=c.key) for c in self.checks],
            "device": {"connected": d.connected, "mode": d.mode, "product": d.product,
                       "name": d.name, "serial": d.serial, "ios": d.ios,
                       "ecid": hex(d.ecid) if d.ecid else ""},
            "builds": [{"build": b.build, "version": b.version, "signed": b.signed,
                        "on_disk": bool(b.path), "size": b.size} for b in self.builds],
            "builds_note": self.builds_note,
            "job": job,
            "firmware": self._firmware_body(),
        }

    def _firmware_body(self) -> dict:
        files = self._inventory()
        try:
            free = shutil.disk_usage(self.ipsw_folder if self.ipsw_folder.exists() else paths.data_dir()).free
        except OSError:
            free = None
        return {"folder": str(self.ipsw_folder), "policy": self.policy, "note": self.fw_note,
                "total": sum(f["size"] for f in files), "free": free,
                "files": [{"name": f["name"], "size": f["size"], "mtime": f["mtime"], "part": f["part"],
                           "version": f["version"], "build": f["build"]} for f in files[:40]]}

    # ------------------------------------------------------------ the beat
    def beat(self) -> dict | None:
        """One check-in. Returns the bench's order, if any."""
        url = bench.bench_url()
        if not url:
            self.status_line = "no bench configured (RD_BENCH / dashboard.json / --bench=)"
            return None
        req = urllib.request.Request(url + "/api/restoredoctor/station",
                                     data=json.dumps(self._body()).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "RestoreDoctor-Station/" + __version__})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                reply = json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.status_line = "bench unreachable at %s (%s)" % (url, str(exc)[:60])
            return None
        self.status_line = "connected to %s - %s" % (url, self.state)
        cmd = reply.get("cmd")
        return cmd if isinstance(cmd, dict) else None

    # ------------------------------------------------------------- orders
    def handle(self, cmd: dict) -> None:
        cid = str(cmd.get("id") or "")
        if not cid or cid == self._ack:
            return                               # already seen: the bench re-sends until acked
        self._ack = cid
        op = cmd.get("op")
        if op == "cancel":
            self._cancel_dl = True
            self.session.cancel()
        elif op == "refresh":
            self._refresh_checks(force=True)
            if self.state == "idle":
                self.session.probe()
                self._refresh_builds(force=True)
        elif op == "fw_open":
            self.open_folder()
        elif op == "fw_policy":
            if cmd.get("policy") in fw.POLICIES:
                self.policy = cmd["policy"]
                self._save_policy(self.policy)
                threading.Thread(target=self._housekeep, daemon=True).start()
        elif op == "fw_delete":
            name = str(cmd.get("name") or "")
            if name in self._in_use():
                self.fw_note = "%s is in use - not deleted" % name
            elif fw.delete_file(self.ipsw_folder, name):
                self.fw_note = "Deleted %s" % name
                self._builds_for = None
            self._inventory(force=True)
        elif op == "restore":
            if self.state != "idle":
                return
            threading.Thread(target=self._restore, args=(cmd,), daemon=True).start()

    def _fail(self, cid: str, msg: str) -> None:
        with self._lock:
            self.job = {"id": cid, "phase": "failed", "msg": msg}
            self.state = "idle"

    def _restore(self, cmd: dict) -> None:
        cid = str(cmd.get("id"))
        erase = bool(cmd.get("erase"))
        if erase and str(cmd.get("confirm") or "").strip().upper() != "ERASE":
            return self._fail(cid, "Erase was not confirmed with the typed word ERASE.")
        bad = [c.label for c in self.checks if c.ok is False and c.key in CRITICAL_CHECKS]
        if bad:
            return self._fail(cid, "This station is not ready: %s. Fix that on the PC first." % ", ".join(bad))
        b = next((x for x in self.builds if x.build == cmd.get("build")), None)
        if b is None:
            return self._fail(cid, "That firmware is no longer in this station's list - refresh and pick again.")
        d = self.session.device
        if not erase and not cmd.get("force") and d.ios and b.version and _ver_tuple(d.ios) > _ver_tuple(b.version):
            return self._fail(cid, "The phone runs iOS %s, newer than %s: an Update cannot downgrade and may "
                                   "lose data." % (d.ios, b.version))
        with self._lock:
            self.job = {"id": cid, "phase": "download" if not b.path else "restore", "pct": 0.0,
                        "msg": "", "build": b.build, "version": b.version, "erase": erase,
                        "file": (b.url or "").rsplit("/", 1)[-1] if not b.path else Path(str(b.path)).name}
            self.state = "downloading" if not b.path else "restoring"
        self._cancel_dl = False
        try:
            if not b.path:
                def prog(f: float, msg: str) -> None:
                    with self._lock:
                        self.job.update({"pct": f, "msg": msg})
                b.path = download(b, self.ipsw_folder, prog, lambda: self._cancel_dl)
            self.session.new_case()
            self.session.ticket = str(cmd.get("ticket") or "")[:60]
            self.session.notes = str(cmd.get("notes") or "")[:2000]
            with self._lock:
                self.job.update({"phase": "restore", "pct": 0.0, "msg": ""})
                self.state = "restoring"
            self.session.start_attempt(b.path, erase)
        except InterruptedError:
            with self._lock:
                self.job.update({"phase": "cancelled", "msg": "Download cancelled"})
                self.state = "idle"
        except Exception as exc:  # noqa: BLE001
            self._fail(cid, "Could not start: %s" % str(exc)[:300])

    # ---------------------------------------------------------------- run
    def tick(self) -> None:
        """One loop pass: facts (never while a phone is being restored), beat, order."""
        if self.state == "idle" and not self.session.running:
            try:
                self.session.probe()
            except Exception:  # noqa: BLE001
                pass
            self._refresh_builds()
        self._refresh_checks()
        cmd = self.beat()
        if cmd:
            self.handle(cmd)

    def run(self) -> None:
        try:
            self.usb.start()
        except Exception:  # noqa: BLE001
            pass
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the station must outlive any one bad pass
                pass
            self._stop.wait(max(0.2, BEAT_S - (time.monotonic() - t0)))
        try:
            self.usb.stop()
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop.set()


def _set_bench(argv: list[str]) -> None:
    """--bench=URL: use it now and remember it (the setup shortcut passes it once)."""
    for a in argv:
        if a.startswith("--bench="):
            url = a.split("=", 1)[1].strip().rstrip("/")
            if url:
                bench._url_cache = url
                try:
                    (paths.data_dir() / "dashboard.json").write_text(json.dumps({"url": url}), encoding="utf-8")
                except OSError:
                    pass


def main(argv: list[str]) -> None:
    _set_bench(argv)
    sim = None
    if paths.simulation_enabled() or "--sim" in argv:
        from .sim import scenarios
        sc = scenarios()
        sim = sc[min(1, len(sc) - 1)]["id"]
    for a in argv:
        if a.startswith("--scenario="):
            sim = a.split("=", 1)[1]
    st = Station(sim_id=sim)
    if "--headless" in argv or (not paths.IS_WINDOWS and not os.environ.get("DISPLAY")):
        print("Restore station %s -> %s  (Ctrl-C stops)" % (st.name, bench.bench_url() or "no bench"))
        try:
            st.run()
        except KeyboardInterrupt:
            st.stop()
        return
    threading.Thread(target=st.run, daemon=True).start()
    try:
        from .ui.station_win import show
    except Exception:  # noqa: BLE001 - no Tk: stay up headless
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            st.stop()
        return
    show(st)
    st.stop()
