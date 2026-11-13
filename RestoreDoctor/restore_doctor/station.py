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
  * a phone that is being restored is never probed — two tools talking to
    one phone can break a restore.

Several phones (0.4): each phone on USB gets its own slot — its own Session,
progress, log and verdict — and restores run side by side. Phones are told
apart by ECID, read passively from the USB list (usbwatch.usb_phone), and
every engine run is pinned to its phone's ECID. While any restore runs, the
only things asked are the OS's device list and details of phones that are
provably NOT the ones restoring; irecovery is not run at all. Two phones that
need the same firmware share one download.
"""
from __future__ import annotations

import json
import os
import platform
import re
import collections
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
from .engine import (DeviceInfo, list_normal_udids, probe_normal, probe_recovery,
                     run_ready_checks)
from . import firmware as fw
from .firmware import StaleList, download, local_builds, merge, signed_builds
from .session import Session

BEAT_S = 2.0
CHECKS_EVERY_S = 60.0
CRITICAL_CHECKS = ("engine", "amds")     # without these a restore cannot even start
GONE_GRACE_S = 6.0            # a phone changing USB mode drops off the bus for a moment
SIM_ECID = 0x0019ED4B21A3C94E
_MERGE = ("ecid", "cpid", "bdid", "product", "board", "name", "serial", "ios", "udid")


def phone_key(d) -> str:
    """A phone's id for orders and the dashboard: its ECID when known (the
    one thing that stays the same through DFU, Recovery and restore mode)."""
    if d.ecid:
        return "e%X" % d.ecid
    if d.udid:
        return "u" + re.sub(r"[^0-9A-Za-z]", "", d.udid)[:40]
    return "x0"


class Phone:
    """One phone's slot on the station: its Session and the order running on it."""

    def __init__(self, station, key: str, device, sim_id: str | None = None):
        self.station, self.key, self.sim_id = station, key, sim_id
        if sim_id:
            from .sim import SimUsbWatch
            self.usb = SimUsbWatch(on_change=None)
        else:
            from .usbwatch import PhoneUsb
            self.usb = PhoneUsb(station.usb, lambda: self.session.device.ecid)
        self.session = Session(station.store, self._notify, usb_watch=self.usb, sim_id=sim_id)
        self.session.device = device
        self.state = "idle"                  # idle | downloading | restoring
        self.job: dict = {}                  # the current / last order's progress
        self.log = collections.deque(maxlen=120)
        self.builds: list = []
        self.builds_note = ""
        self.builds_for: str | None = None
        self.cancel_dl = False
        self.seen = time.monotonic()

    def _notify(self, kind: str, payload: dict) -> None:
        self.station._notify(self, kind, payload)

    @property
    def device(self):
        return self.session.device

    @property
    def busy(self) -> bool:
        return self.state != "idle" or self.session.running


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
        # simulation: one scenario id, or several separated by commas (one phone each)
        self.sim_ids = [x for x in (sim_id or "").split(",") if x]
        self.sim_id = sim_id
        self.sid, self.name = station_id()
        self.store = CaseStore()
        if self.sim_ids:
            from .sim import SimUsbWatch
            self.usb = SimUsbWatch(on_change=None)
        else:
            from .usbwatch import UsbWatch
            self.usb = UsbWatch(on_change=None)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.phones: dict[str, Phone] = {}           # key -> slot, in the order they were plugged in
        self._blank = Phone(self, "", DeviceInfo())  # what "the phone" is while none is connected
        self._rec: dict[int, tuple] = {}             # ecid -> (mode, DeviceInfo, asked irecovery?, t)
        self._norm: dict[str, DeviceInfo] = {}       # udid -> DeviceInfo, while it stays listed
        self.usb_note = ""
        self._dl: dict[str, dict] = {}               # firmware file -> the one download of it
        self.ipsw_folder = Path(ipsw_folder) if ipsw_folder else self._ipsw_folder()
        self.checks: list = []
        self._checks_t = 0.0
        self._ack: str | None = None
        self.policy = self._load_policy()
        self.lookup: dict = {}                      # last Apple error code looked up
        self._inv: list = []
        self._inv_t = 0.0
        self.fw_note = ""                    # what housekeeping last did, for the dashboard
        self.status_line = "starting"        # for the little window / console

    # ------------------------------------------------- "the phone" (first slot)
    # A station with one phone behaves exactly as it did before slots existed;
    # these keep that reading - and the pre-0.4 beat fields - available.
    @property
    def primary(self) -> Phone:
        return next(iter(self.phones.values()), self._blank)

    @property
    def session(self) -> Session:
        return self.primary.session

    @property
    def job(self) -> dict:
        return self.primary.job

    @property
    def state(self) -> str:
        return self.primary.state

    @property
    def builds(self) -> list:
        return self.primary.builds

    @property
    def builds_note(self) -> str:
        return self.primary.builds_note

    @property
    def busy(self) -> bool:
        return any(p.busy for p in list(self.phones.values()))

    def phone(self, key) -> Phone | None:
        """The slot an order is about. No key = the first phone (orders from a
        dashboard older than 0.4 never carry one)."""
        if not key:
            return self.primary if self.phones else None
        return self.phones.get(str(key))

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
        for p in list(self.phones.values()):
            if not p.busy:
                continue
            if p.session.running and p.session.attempts:
                names.append(Path(str(p.session.attempts[-1].ipsw)).name)
            b = p.job.get("file")
            if b:
                names += [b, b + ".part"]
        for name in list(self._dl):
            names += [name, name + ".part"]
        return tuple(names)

    def _housekeep(self, used: str | None = None) -> None:
        try:
            gone = fw.cleanup(self.ipsw_folder, self.policy, keep=self._in_use(), used=used)
        except Exception:  # noqa: BLE001
            gone = []
        if gone:
            self.fw_note = "Removed %s" % ", ".join(gone[:4]) + (" ..." if len(gone) > 4 else "")
            self._relist()                        # on-disk flags changed: re-list
        self._inventory(force=True)

    def _relist(self) -> None:
        for p in list(self.phones.values()):
            p.builds_for = None

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
    def _notify(self, p: Phone, kind: str, payload: dict) -> None:
        if kind == "line":
            p.log.append(str(payload.get("line", ""))[:400])
            return
        if kind == "attempt_started":
            p.log.clear()
            return
        if kind == "attempt_finished":
            s, v = payload.get("summary"), payload.get("verdict")
            ok = getattr(s, "outcome", "") == "success"
            row = self.store.get(p.session.case_id) if p.session.case_id else None
            with self._lock:
                p.job.update({"phase": "done" if ok else "failed", "pct": 1.0 if ok else p.job.get("pct"),
                              "outcome": getattr(s, "outcome", ""),
                              "headline": getattr(v, "headline", "") or "",
                              "case_key": bench.case_key(dict(row)) if row else ""})
                p.state = "idle"
            used = Path(str(p.session.attempts[-1].ipsw)).name if (ok and p.session.attempts) else None
            threading.Thread(target=self._housekeep, args=(used,), daemon=True).start()
        elif kind == "attempt_aborted":
            with self._lock:
                p.job.update({"phase": "failed", "msg": str(payload.get("error", ""))[:300]})
                p.state = "idle"

    # ------------------------------------------------- which phones are here
    def _recovery_info(self, u: dict, ask: bool) -> DeviceInfo:
        """A DFU/Recovery phone from the USB list. With ``ask`` (nothing is
        restoring) irecovery fills in what the USB string lacks, once."""
        ecid, now = u["ecid"], time.monotonic()
        mode, info, asked, t = self._rec.get(ecid, (None, None, False, 0.0))
        if info is not None and mode == u["mode"] and (asked or not ask or now - t < 30):
            return info
        got = None
        if ask:
            try:
                got = probe_recovery(ecid)
            except Exception:  # noqa: BLE001
                got = None
        if got is None:
            got = DeviceInfo(mode=u["mode"], ecid=ecid, cpid=u.get("cpid"), bdid=u.get("bdid"),
                             serial=u.get("serial"))
            m = got.model
            if m:
                got.product, got.name, got.board = m.product, m.name, m.board
        else:
            got.mode = u["mode"]
            got.serial = got.serial or u.get("serial")
        self._rec[ecid] = (u["mode"], got, ask, now)
        return got

    def _enumerate(self) -> list | None:
        """Every phone on USB as a DeviceInfo - or None when that cannot be
        told safely right now (then the slots are left as they are)."""
        if self.sim_ids:
            from .sim import SimDevice
            out = []
            for i, sid in enumerate(self.sim_ids):
                d = SimDevice(sid).probe()
                if d.connected:
                    d.ecid = SIM_ECID + i
                    d.raw = {"sim": sid}
                    out.append(d)
            return out
        from .usbwatch import ecid_from_udid, norm_udid
        busy = [p for p in self.phones.values() if p.busy]
        busy_ecids = {p.device.ecid for p in busy if p.device.ecid}
        busy_udids = {norm_udid(p.device.udid) for p in busy if p.device.udid}
        listed = list(self.usb.phones) if getattr(self.usb, "can_list", False) else None
        out: list = []
        # --- DFU / Recovery: from the USB list, each with its own ECID
        rec = [u for u in (listed or []) if u["mode"] != "Normal"]
        named = [u for u in rec if u.get("ecid")]
        for u in named:
            out.append(self._recovery_info(u, ask=not busy))
        self._rec = {e: v for e, v in self._rec.items() if e in {u["ecid"] for u in named}}
        if (listed is None or len(named) < len(rec)) and not busy:
            # no usable USB list here: the one-phone probe, as before 0.4
            try:
                d = probe_recovery()
            except Exception:  # noqa: BLE001
                d = None
            if d is not None and d.ecid not in {x.ecid for x in out}:
                out.append(d)
        elif listed is None and busy:
            return None
        # --- normal mode: the Apple service's list, then each phone by UDID
        udids = list_normal_udids()
        if udids is None:
            return None if (busy or self._norm) else out
        live = {norm_udid(raw) or raw for raw in udids}
        # Older phones do not show their ECID in restore mode. A busy phone
        # that is visible neither in DFU/Recovery nor under a UDID we know is
        # therefore one of the unnamed UDIDs - and then none of those is touched.
        hidden = sum(1 for p in busy if p.device.ecid not in {u["ecid"] for u in named}
                     and norm_udid(p.device.udid) not in live)
        waiting = 0
        for raw in udids:
            udid = norm_udid(raw) or raw
            guess = ecid_from_udid(udid)
            if guess in busy_ecids or udid in busy_udids:
                continue                              # a phone mid-restore: hands off
            if udid not in self._norm:
                if hidden and guess is None:
                    waiting += 1                      # could BE a restoring phone: do not touch
                    continue
                try:
                    self._norm[udid] = probe_normal(raw)
                except Exception:  # noqa: BLE001
                    continue
            out.append(self._norm[udid])
        self._norm = {u: d for u, d in self._norm.items() if u in live}
        extra = max(0, waiting - hidden)
        self.usb_note = ("%d more phone%s plugged in - identified when the running restore finishes."
                         % (extra, " is" if extra == 1 else "s are")) if extra else ""
        return out

    def _sync(self, devs: list | None) -> None:
        """Bring the slots in line with what is on USB."""
        if devs is None:
            return
        now = time.monotonic()
        seen = set()
        for d in devs:
            key = phone_key(d)
            p = self.phones.get(key)
            if p is None and d.udid:                 # same phone, now known by its ECID
                old = next((x for x in self.phones.values() if x.device.udid == d.udid), None)
                if old is not None:
                    self.phones = {(key if v is old else k): v for k, v in self.phones.items()}
                    old.key, p = key, old
            if p is None:
                p = self.phones[key] = Phone(self, key, d, sim_id=(d.raw or {}).get("sim"))
                if not self.sim_ids:
                    self.usb.track(d.ecid)
            elif p.busy:
                p.device.mode = d.mode               # a restoring phone: only its USB mode
            elif p.device is not d:
                p.device.mode = d.mode
                for f in _MERGE:
                    v = getattr(d, f)
                    if v is not None:
                        setattr(p.device, f, v)
            if not self.sim_ids and d.udid and d.ecid:
                self.usb.know(d.udid, d.ecid)
            p.seen = now
            seen.add(key)
        for key, p in list(self.phones.items()):
            if key in seen:
                continue
            if p.busy:
                continue                             # mid-restore it hops modes; the engine has it
            if now - p.seen < GONE_GRACE_S:
                continue
            if p.job.get("phase"):
                p.device.mode = "None"               # unplugged: its result stays until New phone
            else:
                self._drop(p)
        done = [p for p in self.phones.values() if not p.device.connected and not p.busy]
        for p in done[:-6]:                          # a long day: keep the last few results
            self._drop(p)

    def _drop(self, p: Phone) -> None:
        self.phones.pop(p.key, None)
        if not self.sim_ids:
            self.usb.untrack(p.device.ecid)

    # -------------------------------------------------------------- facts
    def _refresh_checks(self, force: bool = False) -> None:
        if force or time.monotonic() - self._checks_t > CHECKS_EVERY_S or not self.checks:
            if self.sim_ids:
                self.checks = []
            else:
                try:
                    self.checks = run_ready_checks(self.ipsw_folder)
                except Exception:  # noqa: BLE001 - a probe must never stop the loop
                    pass
            self._checks_t = time.monotonic()

    def _refresh_builds(self, p: Phone | None = None, force: bool = False) -> None:
        if p is None:
            for x in list(self.phones.values()):
                if not x.busy:
                    self._refresh_builds(x, force)
            return
        product = p.device.product
        if not product:
            p.builds, p.builds_note, p.builds_for = [], "", None
            return
        if not force and product == p.builds_for:
            return
        local = local_builds(self.ipsw_folder, product)
        note = ""
        if self.sim_ids:
            from .firmware import Build
            got = merge([Build(product, "26.0.1", "23A355", True, url=None, path=Path("simulated.ipsw"))], local)
        else:
            try:
                got = merge(signed_builds(product), local)
            except StaleList as stale:
                got, note = merge(stale.builds, local), str(stale)
            except Exception as exc:  # noqa: BLE001
                got, note = local, "Could not reach api.ipsw.me (%s); showing local files only." % exc
        p.builds, p.builds_note, p.builds_for = got, note, product

    def _job_body(self, p: Phone) -> tuple[dict, str]:
        with self._lock:
            job = dict(p.job)
            state = p.state
        if p.session.attempts:
            att = p.session.attempts[-1]
            s = att.parser.summary()
            if state == "restoring":
                job.update({"pct": s.progress, "progress_step": s.progress_step})
            sig: dict = {}
            for h in s.hits:                      # one line per signal, repeats counted
                e = sig.setdefault(h.title, {"title": h.title, "kind": h.kind, "n": 0})
                e["n"] += 1
            try:
                tl = [[int(t), m] for t, m in p.usb.since(att.started_mono)[-12:]]
            except Exception:  # noqa: BLE001
                tl = []
            job.update({"attempt": att.n, "stage": s.stage, "max_stage": s.max_stage,
                        "step": s.step_plain or s.step, "activity": s.activity,
                        "outcome": s.outcome, "ipsw": Path(str(att.ipsw)).name,
                        "signals": list(sig.values())[:20], "usb_tl": tl,
                        "log": list(p.log)[-80:]})
        return job, state

    def _phone_body(self, p: Phone) -> dict:
        d = p.device
        job, state = self._job_body(p)
        return {
            "key": p.key, "state": state,
            "device": {"connected": d.connected, "mode": d.mode, "product": d.product,
                       "name": d.name, "serial": d.serial, "ios": d.ios,
                       "ecid": hex(d.ecid) if d.ecid else ""},
            "builds": [{"build": b.build, "version": b.version, "signed": b.signed,
                        "on_disk": bool(b.path), "size": b.size} for b in p.builds],
            "builds_note": p.builds_note,
            "job": job,
            "dfu": self._dfu_body(p),
        }

    def _body(self) -> dict:
        slots = list(self.phones.values())
        first = self._phone_body(slots[0] if slots else self._blank)
        d = self.primary.device
        return {
            "station": self.sid, "name": self.name, "version": __version__,
            "platform": platform.system().lower(), "sim": bool(self.sim_ids),
            "ack": self._ack,
            "checks": [dict(c.as_dict(), key=c.key) for c in self.checks],
            # every phone on this station; `key` goes back in an order's "phone"
            "phones": [first] + [self._phone_body(p) for p in slots[1:]] if slots else [],
            # the first phone again, in the fields a pre-0.4 dashboard reads
            "state": first["state"], "device": first["device"], "builds": first["builds"],
            "builds_note": first["builds_note"], "job": first["job"], "dfu": first["dfu"],
            "firmware": self._firmware_body(),
            "usb": {"mode": getattr(self.usb, "current", None) or d.mode,
                    "count": (sum(1 for p in slots if p.device.connected) if self.sim_ids
                              else max(getattr(self.usb, "phone_count", 0),
                                       sum(1 for p in slots if p.device.connected))),
                    "multi": True, "note": self.usb_note,
                    "seen": [{"mode": u["mode"], "id": u["raw"]}
                             for u in (getattr(self.usb, "phones", None) or [])[:12]]},
            "parts": self._parts(),
            "lookup": self.lookup,
        }

    def _dfu_body(self, p: Phone | None = None) -> dict:
        from .models import dfu_steps, recovery_steps
        m = getattr((p or self.primary).device, "model", None)
        return {"name": getattr(m, "name", None) or "this iPhone",
                "dfu": dfu_steps(m), "recovery": recovery_steps(m)}

    def _parts(self) -> list:
        if not hasattr(self, "_parts_cache"):
            try:
                from .kb import get_kb
                self._parts_cache = [[k, v.get("label", k)] for k, v in get_kb().components.items()][:80]
            except Exception:  # noqa: BLE001
                self._parts_cache = []
        return self._parts_cache

    def _lookup(self, code) -> None:
        """Apple error code -> the knowledge base's notes (the app's Code lookup tab)."""
        try:
            n = int(str(code).strip())
        except ValueError:
            self.lookup = {"code": str(code)[:12], "found": False,
                           "msg": "Type the number Apple showed, e.g. 4013."}
            return
        from .kb import get_kb
        kb = get_kb()
        e = kb.lookup_apple_code(n)
        if not e:
            self.lookup = {"code": n, "found": False,
                           "msg": "No notes for error %d yet. Run the restore here to see the step it fails on." % n}
            return
        self.lookup = {"code": n, "found": True, "kind": e.get("kind"), "meaning": e.get("meaning"),
                       "parts": [kb.part_label(p["part"]) for p in sorted(e.get("parts") or [], key=lambda x: -x["w"])],
                       "notes": e.get("notes") or [], "sources": e.get("sources") or []}

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
        n = sum(1 for p in list(self.phones.values()) if p.busy)
        self.status_line = "connected to %s - %s" % (url, ("%d restoring" % n) if n else "idle")
        cmd = reply.get("cmd")
        return cmd if isinstance(cmd, dict) else None

    # ------------------------------------------------------------- orders
    def handle(self, cmd: dict) -> None:
        cid = str(cmd.get("id") or "")
        if not cid or cid == self._ack:
            return                               # already seen: the bench re-sends until acked
        self._ack = cid
        op = cmd.get("op")
        p = self.phone(cmd.get("phone"))         # the slot the order names (or the first phone)
        if op == "cancel":
            if p is not None:
                p.cancel_dl = True
                p.session.cancel()
        elif op == "refresh":
            self._refresh_checks(force=True)
            if not self.busy:
                self._rec, self._norm = {}, {}
                self._sync(self._enumerate())
            self._refresh_builds(force=True)
        elif op == "lookup":
            self._lookup(cmd.get("code"))
        elif op == "new_phone":
            if p is not None and not p.busy:
                p.session.new_case()
                p.log.clear()
                with self._lock:
                    p.job = {}
                if not p.device.connected:
                    self._drop(p)
        elif op == "record_fix":
            # only a case of THIS station's own store, matched by its bench key
            key = str(cmd.get("key") or "")
            try:
                row = self.store.get(int(key.rsplit("-", 1)[1]))
            except (ValueError, IndexError):
                row = None
            if row is not None and bench.case_key(dict(row)) == key and cmd.get("part"):
                self.store.record_fix(row["id"], str(cmd["part"])[:60], str(cmd.get("notes") or "")[:1000])
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
                self._relist()
            self._inventory(force=True)
        elif op == "restore":
            if p is None:
                # the phone the order names is not on this station (any more):
                # say so on the first slot rather than restore a different phone
                if self.phones and not self.primary.busy:
                    self._fail(self.primary, cid, "That phone is no longer connected to this station.")
                return
            if p.busy:
                return
            threading.Thread(target=self._restore, args=(cmd,), daemon=True).start()

    def _fail(self, p: Phone, cid: str, msg: str) -> None:
        with self._lock:
            p.job = {"id": cid, "phase": "failed", "msg": msg}
            p.state = "idle"

    def _firmware(self, p: Phone, b) -> Path:
        """The build's IPSW on disk, downloading it if needed. Phones that
        want the same file share ONE download; it stops only when every phone
        waiting on it has been cancelled."""
        name = (b.url or "").rsplit("/", 1)[-1]
        with self._lock:
            dl = self._dl.get(name)
            mine = dl is None
            if mine:
                dl = self._dl[name] = {"ev": threading.Event(), "users": {p.key}, "pct": 0.0, "msg": "",
                                       "path": None, "err": None}
            else:
                dl["users"].add(p.key)
        if mine:
            def prog(f: float, msg: str) -> None:
                dl["pct"], dl["msg"] = f, msg
                with self._lock:
                    if p.state == "downloading":
                        p.job.update({"pct": f, "msg": msg})

            def stop() -> bool:
                if p.cancel_dl:
                    dl["users"].discard(p.key)
                return not dl["users"]
            try:
                dl["path"] = download(b, self.ipsw_folder, prog, stop)
            except BaseException as exc:  # noqa: BLE001 - handed to every waiting phone
                dl["err"] = exc
            finally:
                with self._lock:
                    self._dl.pop(name, None)
                dl["ev"].set()
            if p.cancel_dl:
                raise InterruptedError("Download cancelled")
        else:
            while not dl["ev"].wait(0.4):
                if p.cancel_dl:
                    dl["users"].discard(p.key)
                    raise InterruptedError("Download cancelled")
                with self._lock:
                    p.job.update({"pct": dl["pct"], "msg": dl["msg"]})
        if dl["err"] is not None:
            raise dl["err"]
        return dl["path"]

    def _restore(self, cmd: dict) -> None:
        cid = str(cmd.get("id"))
        p = self.phone(cmd.get("phone"))
        if p is None or p.busy:
            return
        erase = bool(cmd.get("erase"))
        if erase and str(cmd.get("confirm") or "").strip().upper() != "ERASE":
            return self._fail(p, cid, "Erase was not confirmed with the typed word ERASE.")
        bad = [c.label for c in self.checks if c.ok is False and c.key in CRITICAL_CHECKS]
        if bad:
            return self._fail(p, cid, "This station is not ready: %s. Fix that on the PC first." % ", ".join(bad))
        b = next((x for x in p.builds if x.build == cmd.get("build")), None)
        if b is None:
            return self._fail(p, cid, "That firmware is no longer in this station's list - refresh and pick again.")
        d = p.device
        if not d.connected:
            return self._fail(p, cid, "That phone is no longer connected to this station.")
        others = [x for x in self.phones.values() if x is not p and x.device.connected]
        if others and not d.ecid and not d.udid:
            # with nothing to pin the engine to, it would take whichever phone it finds first
            return self._fail(p, cid, "This phone cannot be told apart from the other phone on USB. "
                                      "Unplug the others, or reconnect this one, then start again.")
        if not erase and not cmd.get("force") and d.ios and b.version and _ver_tuple(d.ios) > _ver_tuple(b.version):
            return self._fail(p, cid, "The phone runs iOS %s, newer than %s: an Update cannot downgrade and may "
                                      "lose data." % (d.ios, b.version))
        with self._lock:
            p.job = {"id": cid, "phase": "download" if not b.path else "restore", "pct": 0.0,
                     "msg": "", "build": b.build, "version": b.version, "erase": erase,
                     "file": (b.url or "").rsplit("/", 1)[-1] if not b.path else Path(str(b.path)).name}
            p.state = "downloading" if not b.path else "restoring"
        p.cancel_dl = False
        try:
            if not b.path:
                b.path = self._firmware(p, b)
                self._relist()                       # other phones' lists: it is on disk now
            p.session.new_case()
            p.session.ticket = str(cmd.get("ticket") or "")[:60]
            p.session.notes = str(cmd.get("notes") or "")[:2000]
            with self._lock:
                p.job.update({"phase": "restore", "pct": 0.0, "msg": ""})
                p.state = "restoring"
            if hasattr(p.usb, "t0"):
                p.usb.t0 = time.monotonic()
            p.session.start_attempt(b.path, erase)
        except InterruptedError:
            with self._lock:
                p.job.update({"phase": "cancelled", "msg": "Download cancelled"})
                p.state = "idle"
        except Exception as exc:  # noqa: BLE001
            self._fail(p, cid, "Could not start: %s" % str(exc)[:300])

    # ---------------------------------------------------------------- run
    def tick(self) -> bool:
        """One loop pass: which phones are here (a restoring phone is never probed), beat, order."""
        try:
            self._sync(self._enumerate())
        except Exception:  # noqa: BLE001
            pass
        self._refresh_builds()
        self._refresh_checks()
        cmd = self.beat()
        if cmd:
            self.handle(cmd)
        return bool(cmd)

    def run(self) -> None:
        try:
            self.usb.start()
        except Exception:  # noqa: BLE001
            pass
        while not self._stop.is_set():
            t0 = time.monotonic()
            got = False
            try:
                got = self.tick()
            except Exception:  # noqa: BLE001 - the station must outlive any one bad pass
                pass
            # an order just arrived: check in again at once - the ack frees the
            # next order waiting behind it (two phones started back to back)
            self._stop.wait(0.3 if got else max(0.2, BEAT_S - (time.monotonic() - t0)))
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
