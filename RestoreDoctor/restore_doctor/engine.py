"""Runs the compiled engine: ready checks, device probing, and restore jobs."""
from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import paths
from .models import DeviceModel, find_by_chip, find_by_product

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env() -> dict:
    env = dict(os.environ)
    eng = str(paths.engine_dir())
    env["PATH"] = eng + os.pathsep + env.get("PATH", "")
    lib = paths.engine_dir() / "lib"
    if lib.exists():  # Linux test builds keep shared libs here
        env["LD_LIBRARY_PATH"] = str(lib) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    ca = paths.engine_dir() / "curl-ca-bundle.crt"
    if ca.exists():
        env.setdefault("CURL_CA_BUNDLE", str(ca))
    return env


def run_tool(name: str, args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    exe = paths.engine_exe(name)
    if not exe.exists():
        return 127, "", f"{exe.name} not found in {exe.parent}"
    try:
        p = subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=_env(), stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"{name} timed out after {timeout:.0f}s"
    except OSError as exc:
        return 126, "", str(exc)


# --------------------------------------------------------------- ready checks

@dataclass
class CheckResult:
    key: str
    label: str
    ok: bool | None          # None = could not tell / not applicable
    detail: str
    fix: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "label": self.label, "detail": self.detail, "fix": self.fix}


def check_engine() -> CheckResult:
    code, out, err = run_tool("idevicerestore", ["-v"], timeout=8)
    if code == 0 and "idevicerestore" in (out + err):
        return CheckResult("engine", "Restore engine", True, (out or err).strip().splitlines()[0])
    return CheckResult("engine", "Restore engine", False, (err or out).strip()[:200] or "not found",
                       "Build the engine with tools/build_engine.sh (MSYS2) or download the release zip, "
                       "then put the engine folder next to RestoreDoctor.exe.")


def check_amds() -> CheckResult:
    label = "Apple Mobile Device Service"
    if paths.IS_WINDOWS:
        try:
            with socket.create_connection(("127.0.0.1", 27015), timeout=1.5):
                return CheckResult("amds", label, True, "Listening on port 27015")
        except OSError:
            return CheckResult("amds", label, False, "Nothing is listening on port 27015",
                               "Install the Apple Devices app (Microsoft Store) or iTunes, then restart "
                               "'Apple Mobile Device Service' in Windows Services.")
    sock = Path("/var/run/usbmuxd")
    if sock.exists():
        return CheckResult("amds", "usbmuxd", True, "Socket present")
    return CheckResult("amds", "usbmuxd", False, "No usbmuxd socket", "Install and start usbmuxd.")


def check_driver() -> CheckResult:
    label = "Apple USB driver"
    if not paths.IS_WINDOWS:
        return CheckResult("driver", label, None, "Not needed on this system")
    try:
        p = subprocess.run(["pnputil", "/enum-drivers"], capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        text = p.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("driver", label, None, f"Could not list drivers: {exc}")
    if re.search(r"usbaapl|Apple Inc\.|Provider Name:\s*Apple", text, re.I):
        return CheckResult("driver", label, True, "Apple driver package installed")
    return CheckResult("driver", label, False, "No Apple driver package found",
                       "Install the Apple Devices app (Microsoft Store) or iTunes. Phones in DFU and "
                       "Recovery mode are reached through Apple's driver.")


def check_signing() -> CheckResult:
    """Reachability only: nothing is sent, so an unverified retry is safe and
    tells a proxy that inspects HTTPS apart from no connection at all. (The
    engine itself does not verify the signing server's certificate.)"""
    label = "Apple signing server"
    url = "https://gs.apple.com/TSS/controller?action=2"
    for verify in (True, False):
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "RestoreDoctor"})
        ctx = None if verify else ssl._create_unverified_context()  # noqa: S323 - reachability probe
        try:
            with urllib.request.urlopen(req, timeout=6, context=ctx) as r:
                code = r.status
        except urllib.error.HTTPError as e:  # any HTTP answer means we reached it
            code = e.code
        except urllib.error.URLError as exc:
            if verify and isinstance(exc.reason, ssl.SSLError):
                continue
            return CheckResult("signing", label, False, f"Not reachable: {exc.reason}",
                               "Check the internet connection, firewall and proxy settings.")
        except OSError as exc:
            return CheckResult("signing", label, False, f"Not reachable: {exc}",
                               "Check the internet connection, firewall and proxy settings.")
        note = "" if verify else " - certificate not verified; a proxy may be inspecting HTTPS"
        return CheckResult("signing", label, True, f"Reachable (HTTP {code}){note}")
    return CheckResult("signing", label, False, "Not reachable")


def hosts_file() -> Path:
    if paths.IS_WINDOWS:
        return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def check_hosts() -> CheckResult:
    label = "Hosts file"
    path = hosts_file()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return CheckResult("hosts", label, None, "Could not read the hosts file")
    bad = [ln.strip() for ln in lines if "gs.apple.com" in ln and not ln.strip().startswith("#")]
    if bad:
        return CheckResult("hosts", label, False, f"Redirects Apple's signing server: {bad[0]}",
                           f"Remove the gs.apple.com line from {path} (needs admin), then retry.")
    return CheckResult("hosts", label, True, "No gs.apple.com redirect")


def check_disk(ipsw_folder: Path | None = None) -> CheckResult:
    """The engine unpacks into the app data folder; IPSWs land in the IPSW folder,
    which may be on another drive. Report the tighter of the two."""
    label = "Free disk space"
    places = [paths.data_dir()] + ([ipsw_folder] if ipsw_folder and ipsw_folder.exists() else [])
    free, where = min((shutil.disk_usage(p).free / 1e9, p) for p in places)
    if free < 20:
        return CheckResult("disk", label, False, f"{free:.0f} GB free on {where.anchor or where}",
                           "Restores need about 20 GB free for the IPSW and its extracted files.")
    return CheckResult("disk", label, True, f"{free:.0f} GB free")


def run_ready_checks(ipsw_folder: Path | None = None) -> list[CheckResult]:
    return [check_engine(), check_amds(), check_driver(), check_signing(), check_hosts(),
            check_disk(ipsw_folder)]


# --------------------------------------------------------------- device probe

@dataclass
class DeviceInfo:
    mode: str = "None"           # None | DFU | Recovery | Normal | Restore | WTF
    ecid: int | None = None
    cpid: int | None = None
    bdid: int | None = None
    product: str | None = None
    board: str | None = None
    name: str | None = None
    serial: str | None = None
    ios: str | None = None
    udid: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def model(self) -> DeviceModel | None:
        if self.product:
            m = find_by_product(self.product)
            if m:
                return m
        if self.cpid is not None and self.bdid is not None:
            return find_by_chip(self.cpid, self.bdid)
        return None

    @property
    def connected(self) -> bool:
        return self.mode not in ("None", "")


def parse_irecovery(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().upper()] = v.strip()
    return out


def probe_device() -> DeviceInfo:
    """Find a phone in DFU/Recovery (irecovery) or normal mode (lockdown)."""
    code, out, _ = run_tool("irecovery", ["-q"], timeout=6)
    if code == 0 and "ECID" in out:
        q = parse_irecovery(out)
        info = DeviceInfo(raw=q)
        mode = q.get("MODE", "")
        info.mode = "DFU" if "DFU" in mode.upper() else ("Recovery" if "RECOVERY" in mode.upper() else mode or "DFU")
        try:
            info.ecid = int(q.get("ECID", "0"), 16)
            info.cpid = int(q.get("CPID", "0"), 16)
            info.bdid = int(q.get("BDID", "0"), 16)
        except ValueError:
            pass
        info.product = q.get("PRODUCT") or None
        info.board = q.get("MODEL") or None
        info.name = q.get("NAME") or None
        if q.get("SRNM") not in (None, "", "N/A"):
            info.serial = q["SRNM"]
        if not info.name and info.model:
            info.name = info.model.name
        return info
    code, out, _ = run_tool("idevice_id", ["-l"], timeout=6)
    udids = [ln.strip() for ln in out.splitlines() if ln.strip()] if code == 0 else []
    if udids:
        info = DeviceInfo(mode="Normal", udid=udids[0])
        for args in (["-u", udids[0]], ["-s", "-u", udids[0]]):
            code, out, _ = run_tool("ideviceinfo", args, timeout=10)
            if code == 0 and out.strip():
                kv = {}
                for line in out.splitlines():
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        kv[k.strip()] = v.strip()
                info.raw = kv
                info.product = kv.get("ProductType")
                info.ios = kv.get("ProductVersion")
                info.serial = kv.get("SerialNumber")
                if kv.get("UniqueChipID"):
                    try:
                        info.ecid = int(kv["UniqueChipID"])
                    except ValueError:
                        pass
                break
        if info.model:
            info.name = info.model.name
        return info
    return DeviceInfo()


# --------------------------------------------------------------- restore job

class RestoreJob:
    """One engine run. Lines arrive on worker threads via ``on_line(line, stream)``;
    ``on_exit(exit_code, cancelled)`` fires once when the process ends."""

    def __init__(self, ipsw: Path, erase: bool, ecid: int | None, workdir: Path,
                 on_line: Callable[[str, str], None], on_exit: Callable[[int, bool], None],
                 cache_dir: Path | None = None):
        self.ipsw, self.erase, self.ecid, self.workdir = Path(ipsw), erase, ecid, Path(workdir)
        self.on_line, self.on_exit = on_line, on_exit
        self.cache_dir = cache_dir
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self.started_at: float | None = None
        self.raw_log = self.workdir / "engine.log"

    def command(self) -> list[str]:
        cmd = [str(paths.engine_exe("idevicerestore")), "-y", "-d", "-P", "--logfile=-"]
        if self.erase:
            cmd.append("-e")
        if self.ecid:
            cmd += ["-i", hex(self.ecid)]
        if self.cache_dir:
            cmd += ["-C", str(self.cache_dir)]
        cmd.append(str(self.ipsw))
        return cmd

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.monotonic()
        self._proc = subprocess.Popen(
            self.command(), cwd=str(self.workdir), env=_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=NO_WINDOW)
        log = open(self.raw_log, "a", encoding="utf-8")
        lock = threading.Lock()

        def pump(stream, name):
            for line in stream:
                with lock:
                    log.write(line if name == "err" or line.startswith("progress:") else "")
                    log.flush()
                self.on_line(line, name)

        threads = [threading.Thread(target=pump, args=(self._proc.stdout, "out"), daemon=True),
                   threading.Thread(target=pump, args=(self._proc.stderr, "err"), daemon=True)]
        for t in threads:
            t.start()

        def waiter():
            code = self._proc.wait()
            for t in threads:
                t.join(timeout=5)
            log.close()
            self.on_exit(code, self._cancelled)

        threading.Thread(target=waiter, daemon=True).start()

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
