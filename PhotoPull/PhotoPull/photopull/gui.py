"""PhotoPull Tk GUI. All device I/O runs on a background asyncio loop."""
from __future__ import annotations

import asyncio
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import engine
from .engine import DeviceInfo, MediaItem, Options, UserFacingError

APP_NAME = "PhotoPull"
APP_VERSION = "1.0"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_eta(sec: float) -> str:
    if sec <= 0 or sec != sec or sec > 360000:
        return "--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


class AsyncRunner:
    """One long-lived asyncio loop on a daemon thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} - iPhone & iPad Photo/Video Extractor")
        self.minsize(720, 600)
        self.runner = AsyncRunner()
        self.ui_q: "queue.Queue[tuple]" = queue.Queue()
        self.cancel = threading.Event()

        self.devices: list[DeviceInfo] = []
        self.device: Optional[DeviceInfo] = None
        self.lockdown = None
        self.afc = None
        self.items: list[MediaItem] = []
        self.busy = False
        self.speed_samples: list[tuple[float, int]] = []
        self.last_dest_root: Optional[Path] = None

        self._build()
        self.after(100, self._pump)
        self.after(200, self.refresh_devices)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Big.TButton", padding=(14, 6))
        style.configure("Head.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Sum.TLabel", font=("Segoe UI", 10))

        pad = {"padx": 10, "pady": 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="Pull photos & videos off an iPhone / iPad", style="Head.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        # device
        ttk.Label(root, text="Device:").grid(row=1, column=0, sticky="w", **pad)
        self.dev_var = tk.StringVar()
        self.dev_combo = ttk.Combobox(root, textvariable=self.dev_var, state="readonly")
        self.dev_combo.grid(row=1, column=1, sticky="ew", **pad)
        self.dev_combo.bind("<<ComboboxSelected>>", lambda e: self._device_changed())
        self.refresh_btn = ttk.Button(root, text="Refresh", command=self.refresh_devices)
        self.refresh_btn.grid(row=1, column=2, sticky="ew", **pad)

        # destination
        ttk.Label(root, text="Save to:").grid(row=2, column=0, sticky="w", **pad)
        default_dest = Path.home() / "Pictures" / "PhotoPull"
        self.dest_var = tk.StringVar(value=str(default_dest))
        ttk.Entry(root, textvariable=self.dest_var).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse...", command=self._browse).grid(row=2, column=2, sticky="ew", **pad)

        # options
        opts = ttk.LabelFrame(root, text="What to copy", padding=8)
        opts.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 4))
        self.v_photos = tk.BooleanVar(value=True)
        self.v_videos = tk.BooleanVar(value=True)
        self.v_live = tk.BooleanVar(value=True)
        self.v_aae = tk.BooleanVar(value=False)
        for i, (txt, var) in enumerate([("Photos", self.v_photos), ("Videos", self.v_videos),
                                        ("Live Photo motion clips", self.v_live),
                                        ("Edit sidecars (.AAE)", self.v_aae)]):
            ttk.Checkbutton(opts, text=txt, variable=var, command=self._update_summary).grid(
                row=0, column=i, sticky="w", padx=(0, 18))

        lay = ttk.LabelFrame(root, text="Folder layout & format", padding=8)
        lay.grid(row=4, column=0, columnspan=3, sticky="ew", padx=10, pady=4)
        self.v_layout = tk.StringVar(value=engine.LAYOUT_DATE)
        for i, (txt, val) in enumerate([("By year / month", engine.LAYOUT_DATE),
                                        ("Keep phone folders (100APPLE...)", engine.LAYOUT_DCIM),
                                        ("Everything in one folder", engine.LAYOUT_FLAT)]):
            ttk.Radiobutton(lay, text=txt, value=val, variable=self.v_layout,
                            command=self._update_summary).grid(row=0, column=i, sticky="w", padx=(0, 18))

        ttk.Label(lay, text="HEIC photos:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.heic_choices = {
            "Keep original HEIC": engine.HEIC_KEEP,
            "HEIC + a JPG copy": engine.HEIC_BOTH,
            "Convert to JPG only": engine.HEIC_JPG,
        }
        self.v_heic = tk.StringVar(value="Keep original HEIC")
        heic_box = ttk.Combobox(lay, textvariable=self.v_heic, state="readonly",
                                values=list(self.heic_choices), width=24)
        heic_box.grid(row=1, column=1, sticky="w", pady=(8, 0))
        heic_box.bind("<<ComboboxSelected>>", lambda e: self._update_summary())
        if not engine.heic_supported():
            heic_box.configure(state="disabled")

        # actions
        act = ttk.Frame(root)
        act.grid(row=5, column=0, columnspan=3, sticky="ew", padx=10, pady=(10, 4))
        self.scan_btn = ttk.Button(act, text="1. Connect & Scan", style="Big.TButton", command=self.scan)
        self.scan_btn.pack(side="left")
        self.start_btn = ttk.Button(act, text="2. Copy to PC", style="Big.TButton", command=self.start,
                                    state="disabled")
        self.start_btn.pack(side="left", padx=8)
        self.cancel_btn = ttk.Button(act, text="Cancel", command=self.do_cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        self.open_btn = ttk.Button(act, text="Open folder", command=self.open_folder)
        self.open_btn.pack(side="right")

        self.summary_var = tk.StringVar(value="Plug in a trusted iPhone/iPad, unlock it, then click Connect & Scan.")
        ttk.Label(root, textvariable=self.summary_var, style="Sum.TLabel", wraplength=680, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 2))

        self.pbar = ttk.Progressbar(root, mode="determinate", maximum=1000)
        self.pbar.grid(row=7, column=0, columnspan=3, sticky="ew", padx=10, pady=4)
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(root, textvariable=self.status_var).grid(row=8, column=0, columnspan=3, sticky="w", padx=10)

        logf = ttk.Frame(root)
        logf.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=10, pady=(6, 0))
        root.rowconfigure(9, weight=1)
        self.log = tk.Text(logf, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ------------------------------------------------------------------ helpers
    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def post(self, *msg) -> None:
        """Thread-safe: queue a UI update."""
        self.ui_q.put(msg)

    def _pump(self) -> None:
        try:
            while True:
                msg = self.ui_q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "progress":
                    self._show_progress(*msg[1:])
                elif kind == "call":
                    msg[1](*msg[2:])
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _set_busy(self, busy: bool, copying: bool = False) -> None:
        self.busy = busy
        st = "disabled" if busy else "normal"
        self.scan_btn.configure(state=st)
        self.refresh_btn.configure(state=st)
        self.dev_combo.configure(state="disabled" if busy else "readonly")
        self.start_btn.configure(state="disabled" if busy or not self.items else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.dest_var.get() or str(Path.home()))
        if d:
            self.dest_var.set(d)
            self._update_summary()

    def _options(self) -> Options:
        return Options(
            dest=Path(self.dest_var.get()).expanduser(),
            photos=self.v_photos.get(), videos=self.v_videos.get(),
            live=self.v_live.get(), sidecars=self.v_aae.get(),
            layout=self.v_layout.get(),
            heic=self.heic_choices.get(self.v_heic.get(), engine.HEIC_KEEP),
        )

    def _selected_device(self) -> Optional[DeviceInfo]:
        i = self.dev_combo.current()
        return self.devices[i] if 0 <= i < len(self.devices) else None

    def _device_changed(self) -> None:
        if self.device and self._selected_device() and self.device.serial != self._selected_device().serial:
            self._close_session()
            self.items = []
            self.start_btn.configure(state="disabled")
            self.summary_var.set("Device changed - click Connect & Scan.")

    # ------------------------------------------------------------------ devices
    def refresh_devices(self) -> None:
        if self.busy:
            return
        self.status_var.set("Looking for devices...")

        def done(fut):
            try:
                devs = fut.result()
                self.post("call", self._got_devices, devs, None)
            except Exception as e:
                self.post("call", self._got_devices, [], e)

        self.runner.submit(engine.find_devices()).add_done_callback(done)

    def _got_devices(self, devs: list[DeviceInfo], err: Optional[Exception]) -> None:
        prev = self._selected_device()
        self.devices = devs
        self.dev_combo["values"] = [f"{d.label}  [{d.connection}]" for d in devs]
        if err:
            self.status_var.set("Device service unavailable")
            self._log(str(err))
            self.dev_var.set("")
            return
        if not devs:
            self.dev_var.set("")
            self.status_var.set("No device found - plug in and unlock your iPhone/iPad, then Refresh.")
            return
        idx = 0
        if prev:
            idx = next((i for i, d in enumerate(devs) if d.serial == prev.serial), 0)
        self.dev_combo.current(idx)
        self.status_var.set(f"Found {len(devs)} device(s).")

    # ------------------------------------------------------------------ scan
    def scan(self) -> None:
        dev = self._selected_device()
        if not dev:
            self.refresh_devices()
            messagebox.showinfo(APP_NAME, "No device selected. Plug in and unlock the device, then click Refresh.")
            return
        self.cancel.clear()
        self._set_busy(True)
        self.items = []
        self.pbar.configure(mode="indeterminate")
        self.pbar.start(12)
        self.runner.submit(self._scan_async(dev))

    async def _scan_async(self, dev: DeviceInfo) -> None:
        try:
            if self.device is None or self.device.serial != dev.serial or self.afc is None:
                await self._close_session_async()
                self.post("status", "Connecting... if asked, tap 'Trust' on the device and enter the passcode.")
                self.post("log", f"Connecting to {dev.label}")
                self.lockdown = await engine.connect(dev.serial, pair_timeout=90)
                v = self.lockdown.all_values
                dev.name = v.get("DeviceName") or dev.name
                dev.ios = v.get("ProductVersion") or dev.ios
                dev.trusted = True
                self.afc = engine.AfcService(self.lockdown)
                await self.afc.__aenter__()
                self.device = dev
                self.post("log", f"Connected: {dev.label}")
            items = await engine.scan(self.afc, lambda s: self.post("status", s), self.cancel)
            self.items = items
            self.post("call", self._scan_done, None)
        except UserFacingError as e:
            await self._close_session_async()
            self.post("call", self._scan_done, str(e))
        except Exception as e:
            await self._close_session_async()
            self.post("call", self._scan_done, engine._friendly(e))

    def _scan_done(self, err: Optional[str]) -> None:
        self.pbar.stop()
        self.pbar.configure(mode="determinate", value=0)
        self._set_busy(False)
        if err:
            self.status_var.set("Scan failed")
            self._log(err)
            messagebox.showerror(APP_NAME, err)
            return
        if self.cancel.is_set():
            self.status_var.set("Scan cancelled")
            return
        self.status_var.set(f"Scan complete - {len(self.items):,} files in the camera roll.")
        self._log(f"Scan found {len(self.items):,} files")
        if not self.items:
            self.summary_var.set("No photos or videos found in the camera roll. If iCloud Photos uses "
                                 "'Optimize iPhone Storage', originals live in iCloud, not on the device.")
        self._update_summary()
        self.start_btn.configure(state="normal" if self.items else "disabled")

    def _update_summary(self) -> None:
        if not self.items:
            return
        counts: dict[str, list[int]] = {}
        for it in self.items:
            c = counts.setdefault(it.kind, [0, 0])
            c[0] += 1
            c[1] += it.size
        names = {"photo": ("photo", "photos"), "video": ("video", "videos"),
                 "live": ("Live Photo clip", "Live Photo clips"), "sidecar": (".AAE edit", ".AAE edits")}
        parts = [f"{counts[k][0]:,} {names[k][counts[k][0] != 1]} ({human(counts[k][1])})"
                 for k in names if k in counts]
        opt = self._options()
        sel = engine.select(self.items, opt)
        sel_bytes = sum(i.size for i in sel)
        line = "On device: " + " · ".join(parts) + f"\nSelected: {len(sel):,} files, {human(sel_bytes)}"
        try:
            probe = opt.dest
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            free = shutil.disk_usage(probe).free
            line += f"  ·  Free on destination: {human(free)}"
            if free < sel_bytes:
                line += "  (may not fit - files already copied will be skipped)"
        except Exception:
            pass
        self.summary_var.set(line)

    # ------------------------------------------------------------------ copy
    def start(self) -> None:
        if not self.items or self.afc is None:
            return
        opt = self._options()
        sel = engine.select(self.items, opt)
        if not sel:
            messagebox.showinfo(APP_NAME, "Nothing selected - tick Photos and/or Videos.")
            return
        try:
            opt.dest.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Can't create destination folder:\n{e}")
            return
        engine.plan(sel, opt, self.device.name if self.device else "iOS device")
        self.last_dest_root = opt.dest / engine._safe(self.device.name if self.device else "iOS device")
        self.cancel.clear()
        self.speed_samples = []
        self._set_busy(True, copying=True)
        self._log(f"Copying {len(sel):,} files to {self.last_dest_root}")
        self.runner.submit(self._copy_async(sel, opt))

    async def _copy_async(self, sel: list[MediaItem], opt: Options) -> None:
        last = [0.0]

        def on_progress(done, total, b, btotal, name):
            now = time.monotonic()
            if name == "" or now - last[0] > 0.15 or done == total:
                last[0] = now
                self.post("progress", done, total, b, btotal, name)

        t0 = time.monotonic()
        try:
            stats = await engine.copy_all(self.afc, sel, opt, self.cancel, on_progress)
            err = None
        except Exception as e:
            stats, err = None, engine._friendly(e)
        self.post("call", self._copy_done, stats, err, time.monotonic() - t0)

    def _show_progress(self, done, total, b, btotal, name) -> None:
        frac = (b / btotal) if btotal else (done / total if total else 1)
        self.pbar.configure(value=int(frac * 1000))
        now = time.monotonic()
        self.speed_samples.append((now, b))
        self.speed_samples = [s for s in self.speed_samples if now - s[0] < 5]
        speed = 0.0
        if len(self.speed_samples) > 1:
            dt = self.speed_samples[-1][0] - self.speed_samples[0][0]
            if dt > 0:
                speed = (self.speed_samples[-1][1] - self.speed_samples[0][1]) / dt
        eta = (btotal - b) / speed if speed > 0 else 0
        cur = f"  ·  {name}" if name else ""
        self.status_var.set(f"{done:,}/{total:,} files  ·  {human(b)} of {human(btotal)}  ·  "
                            f"{human(speed)}/s  ·  ETA {fmt_eta(eta)}{cur}")

    def _copy_done(self, stats: Optional[engine.Stats], err: Optional[str], secs: float) -> None:
        self._set_busy(False)
        if err:
            self._log(f"Stopped: {err}")
            self.status_var.set("Copy stopped - see log")
            messagebox.showerror(APP_NAME, f"{err}\n\nRun it again to resume; finished files are skipped.")
            self._close_session()
            return
        cancelled = self.cancel.is_set()
        msg = (f"{'Cancelled' if cancelled else 'Done'}: {stats.copied:,} copied ({human(stats.bytes_copied)}), "
               f"{stats.skipped:,} already there, {stats.failed:,} failed  ·  {fmt_eta(secs)}")
        self._log(msg)
        self.status_var.set(msg)
        if not cancelled:
            self.pbar.configure(value=1000)
        if stats.errors:
            for e in stats.errors[:20]:
                self._log("  " + e)
            p = engine.write_log(self.last_dest_root or Path(self.dest_var.get()), stats, self.device)
            if p:
                self._log(f"Full error list: {p}")
            messagebox.showwarning(APP_NAME, f"{stats.failed:,} file(s) failed. Run Copy again to retry them."
                                   + (f"\n\nDetails: {p}" if p else ""))
        elif not cancelled:
            messagebox.showinfo(APP_NAME, msg)

    def do_cancel(self) -> None:
        self.cancel.set()
        self.status_var.set("Cancelling...")

    def open_folder(self) -> None:
        p = self.last_dest_root if self.last_dest_root and self.last_dest_root.exists() else Path(self.dest_var.get())
        p.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])

    # ------------------------------------------------------------------ teardown
    async def _close_session_async(self) -> None:
        afc, ld = self.afc, self.lockdown
        self.afc = self.lockdown = None
        self.device = None
        for obj in (afc, ld):
            if obj is None:
                continue
            try:
                if hasattr(obj, "aclose"):
                    await obj.aclose()
                else:
                    await obj.close()
            except Exception:
                pass

    def _close_session(self) -> None:
        self.runner.submit(self._close_session_async())

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_NAME, "A copy is running. Stop and quit?"):
            return
        self.cancel.set()
        try:
            self.runner.submit(self._close_session_async()).result(timeout=3)
        except Exception:
            pass
        self.destroy()


def main() -> None:
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()
