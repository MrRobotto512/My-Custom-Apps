"""Restore Doctor main window."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import APP_NAME, __version__, paths
from ..cases import CaseStore
from ..diagnose import Verdict, diagnose
from ..engine import DeviceInfo, run_ready_checks
from ..firmware import Build, StaleList, download, local_builds, merge, read_ipsw, signed_builds
from ..kb import get_kb
from ..logparse import STAGE_PLAIN, STAGES, RestoreLogParser
from ..models import dfu_steps, recovery_steps
from ..session import Session
from ..usbwatch import UsbWatch
from . import theme as T

SETTINGS = "settings.json"


def load_settings() -> dict:
    p = paths.data_dir() / SETTINGS
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(d: dict) -> None:
    (paths.data_dir() / SETTINGS).write_text(json.dumps(d, indent=1), encoding="utf-8")


def log_exception(where: str) -> None:
    """Append the current exception to errors.log (the windowed exe has no console)."""
    try:
        with open(paths.data_dir() / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {where}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def open_path(p: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError:
        webbrowser.open(p.as_uri())


class ScrollFrame(ttk.Frame):
    """A vertically scrolling frame; put children in ``.body``."""

    def __init__(self, master, bg=T.BG):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, width=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        # bar first, so a narrow parent squeezes the canvas rather than hiding the bar
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._wraps: list[tuple[tk.Label, int]] = []   # labels whose wraplength follows the width
        self.canvas.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", lambda e: self._wheel(True))
        self.bind("<Leave>", lambda e: self._wheel(self._pointer_inside()))

    def _pointer_inside(self) -> bool:
        """Leave also fires when the pointer moves onto a child widget; only stop when it is really outside."""
        w = self.winfo_containing(*self.winfo_pointerxy())
        while w is not None:
            if w is self:
                return True
            w = w.master
        return False

    def _wheel(self, on: bool):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        else:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(seq)

    def _on_resize(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        for lab, margin in self._wraps:
            if lab.winfo_exists():
                lab.configure(wraplength=max(200, e.width - margin))

    def wrap(self, label: tk.Label, margin: int) -> tk.Label:
        """Keep ``label`` wrapped to the visible width minus ``margin`` pixels."""
        self._wraps.append((label, margin))
        label.configure(wraplength=max(200, self.canvas.winfo_width() - margin))
        return label

    def _on_wheel(self, e):
        # Text widgets (notes) scroll themselves; don't move the page underneath them.
        if isinstance(e.widget, tk.Text):
            return
        if self.body.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

    def clear(self):
        self._wraps = []
        for w in self.body.winfo_children():
            w.destroy()
        self.canvas.yview_moveto(0)


class App(tk.Tk):
    def __init__(self, sim: bool = False, scenario: str | None = None):
        super().__init__()
        self.sim = sim
        self.settings = load_settings()
        self.title(f"{APP_NAME} {__version__}" + ("  [SIMULATION]" if sim else ""))
        # Tk scales point-size fonts with the display's DPI; scale our pixel
        # sizes to match so the layout holds at 125-200% Windows scaling.
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(self.px(1280), int(sw * 0.94)), min(self.px(820), int(sh * 0.9))
        self.geometry(f"{w}x{h}")
        self.minsize(min(self.px(1100), w), min(self.px(700), h))
        self.fonts = T.Fonts(self)
        T.apply(self, self.fonts)
        self.kb = get_kb()
        self.q: queue.Queue = queue.Queue()
        self.store = CaseStore()
        if sim:
            from ..sim import SimUsbWatch, scenarios
            self.scenarios = scenarios()
            self.sim_id = tk.StringVar(value=scenario or self.scenarios[min(1, len(self.scenarios) - 1)]["id"])
            self.usb = SimUsbWatch(on_change=self._usb_changed)
        else:
            self.scenarios = []
            self.sim_id = tk.StringVar(value="")
            self.usb = UsbWatch(on_change=self._usb_changed)
        self.session = Session(self.store, self._notify, usb_watch=self.usb,
                               sim_id=self.sim_id.get() if sim else None)
        self.builds: list[Build] = []
        self.checks: list = []
        self._probe_pending = False
        self._downloading = threading.Event()
        self._cancel_download = False
        self._viewing_case: int | None = None
        self._signal_counts: dict[str, int] = {}

        self._build_menu()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain)
        self.after(200, self._startup)

    # ================================================================ plumbing
    def px(self, n: float) -> int:
        return int(round(n * getattr(self, "scale", 1.0)))

    def _notify(self, kind: str, payload: dict) -> None:
        self.q.put((kind, payload))

    def _usb_changed(self, t: float, mode: str) -> None:
        self.q.put(("usb", {"t": t, "mode": mode}))

    def _bg(self, fn, kind: str, *args):
        def run():
            try:
                self.q.put((kind, {"result": fn(*args)}))
            except Exception as exc:  # noqa: BLE001 - shown to the tech
                self.q.put((kind, {"error": str(exc)}))
        threading.Thread(target=run, daemon=True).start()

    def _drain(self):
        try:
            for _ in range(400):
                try:
                    kind, payload = self.q.get_nowait()
                except queue.Empty:
                    break
                handler = getattr(self, f"_on_{kind}", None)
                if handler:
                    try:
                        handler(payload)
                    except Exception:  # noqa: BLE001 - one bad event must not stop the UI
                        log_exception(f"handler _on_{kind}")
        finally:
            self.after(80, self._drain)

    def _startup(self):
        self.usb.start()
        self.run_checks()
        self.probe()
        self.refresh_history()

    def _on_close(self):
        if self.session.running and not messagebox.askyesno(
                APP_NAME, "A restore is running. Closing now stops it and can leave the phone in Recovery.\n\n"
                          "Close anyway?"):
            return
        self.session.cancel()
        self.usb.stop()
        self.destroy()

    # ================================================================== menu
    def _build_menu(self):
        m = tk.Menu(self, tearoff=False)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label="Import a restore log...", command=self.import_log)
        f.add_command(label="Choose IPSW folder...", command=self.choose_ipsw_folder)
        f.add_command(label="Open data folder", command=lambda: open_path(paths.data_dir()))
        f.add_separator()
        f.add_command(label="Exit", command=self._on_close)
        m.add_cascade(label="File", menu=f)
        if self.sim:
            s = tk.Menu(m, tearoff=False)
            for sc in self.scenarios:
                s.add_radiobutton(label=sc["title"], value=sc["id"], variable=self.sim_id,
                                  command=self._sim_changed)
            m.add_cascade(label="Simulation", menu=s)
        h = tk.Menu(m, tearoff=False)
        h.add_command(label="DFU helper", command=self.show_dfu_helper)
        h.add_command(label="About", command=lambda: messagebox.showinfo(
            APP_NAME, f"{APP_NAME} {__version__}\n\nRuns iPhone restores with idevicerestore and explains "
                      "why they fail.\n\nSuspects are leads ranked from the restore log, not a final "
                      "diagnosis."))
        m.add_cascade(label="Help", menu=h)
        self.configure(menu=m)

    def _sim_changed(self):
        self.session.sim_id = self.sim_id.get()
        self.probe()

    # ================================================================ layout
    def _build_layout(self):
        top = tk.Frame(self, bg=T.PANEL)
        top.pack(side="top", fill="x")
        tk.Label(top, text=APP_NAME, bg=T.PANEL, fg=T.INK, font=self.fonts.h2).pack(side="left", padx=(16, 6),
                                                                                   pady=10)
        if self.sim:
            tk.Label(top, text=" SIMULATION ", bg=T.WARN, fg="#1a1205", font=self.fonts.bold).pack(side="left")
        self.chips = tk.Frame(top, bg=T.PANEL)
        self.chips.pack(side="left", padx=18)
        ttk.Button(top, text="Re-check", command=self.run_checks).pack(side="right", padx=12, pady=8)
        self.usb_label = tk.Label(top, text="USB: watching...", bg=T.PANEL, fg=T.QUIET, font=self.fonts.small)
        self.usb_label.pack(side="right", padx=10)

        body = tk.Frame(self, bg=T.BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=T.PANEL, width=self.px(360))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self._build_left(left)
        right = tk.Frame(body, bg=T.BG)
        right.pack(side="left", fill="both", expand=True)
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(10, 12))
        self.tab_restore = ttk.Frame(self.nb)
        self.tab_verdict = ttk.Frame(self.nb)
        self.tab_history = ttk.Frame(self.nb)
        self.tab_lookup = ttk.Frame(self.nb)
        self.nb.add(self.tab_restore, text="Restore")
        self.nb.add(self.tab_verdict, text="Verdict")
        self.nb.add(self.tab_history, text="History")
        self.nb.add(self.tab_lookup, text="Code lookup")
        self._build_restore_tab(self.tab_restore)
        self._build_verdict_tab(self.tab_verdict)
        self._build_history_tab(self.tab_history)
        self._build_lookup_tab(self.tab_lookup)

    # ---------------------------------------------------------------- left
    def _section(self, parent, title):
        tk.Label(parent, text=title.upper(), bg=T.PANEL, fg=T.FAINT, font=self.fonts.small).pack(
            anchor="w", padx=16, pady=(14, 4))

    def _build_left(self, panel):
        # The action buttons are packed first so they keep their space on short screens;
        # everything else scrolls above them.
        bottom = tk.Frame(panel, bg=T.PANEL)
        bottom.pack(side="bottom", fill="x", padx=14, pady=14)
        self.start_btn = ttk.Button(bottom, text="Start restore", style="Accent.TButton", command=self.start)
        self.start_btn.pack(fill="x")
        row2 = tk.Frame(bottom, bg=T.PANEL)
        row2.pack(fill="x", pady=(8, 0))
        self.cancel_btn = ttk.Button(row2, text="Stop", style="Danger.TButton", command=self.cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left")
        ttk.Button(row2, text="New phone", command=self.new_phone).pack(side="right")
        tk.Frame(panel, bg=T.LINE, height=1).pack(side="bottom", fill="x")
        self.left_scroll = ScrollFrame(panel, bg=T.PANEL)
        self.left_scroll.pack(side="top", fill="both", expand=True)
        p = self.left_scroll.body

        self._section(p, "Phone")
        card = tk.Frame(p, bg=T.CARD, highlightthickness=1, highlightbackground=T.LINE)
        card.pack(fill="x", padx=14)
        row = tk.Frame(card, bg=T.CARD)
        row.pack(fill="x", padx=12, pady=(12, 2))
        self.mode_badge = tk.Label(row, text=" NOT CONNECTED ", bg=T.FAINT, fg="#0b0d10", font=self.fonts.bold)
        self.mode_badge.pack(side="left")
        ttk.Button(row, text="Detect", command=self.probe).pack(side="right")
        self.dev_name = tk.Label(card, text="Plug in a phone", bg=T.CARD, fg=T.INK, font=self.fonts.h2,
                                 anchor="w")
        self.dev_name.pack(fill="x", padx=12, pady=(6, 0))
        self.dev_meta = tk.Label(card, text="DFU or Recovery mode works best for a restore.", bg=T.CARD,
                                 fg=T.QUIET, font=self.fonts.small, anchor="w", justify="left",
                                 wraplength=self.px(290))
        self.dev_meta.pack(fill="x", padx=12, pady=(2, 10))
        ttk.Button(card, text="How to enter DFU / Recovery", command=self.show_dfu_helper).pack(
            anchor="w", padx=12, pady=(0, 12))

        self._section(p, "Firmware")
        self.fw_var = tk.StringVar()
        self.fw_combo = ttk.Combobox(p, textvariable=self.fw_var, state="readonly")
        self.fw_combo.pack(fill="x", padx=14)
        self.fw_combo.bind("<<ComboboxSelected>>", lambda e: self._fw_selected())
        fr = tk.Frame(p, bg=T.PANEL)
        fr.pack(fill="x", padx=14, pady=(6, 0))
        for i in range(3):
            fr.columnconfigure(i, weight=1, uniform="fw")
        ttk.Button(fr, text="Refresh", command=self.load_builds, padding=(4, 5)).grid(row=0, column=0, sticky="ew")
        ttk.Button(fr, text="Browse...", command=self.browse_ipsw, padding=(4, 5)).grid(row=0, column=1, sticky="ew",
                                                                                       padx=6)
        self.dl_btn = ttk.Button(fr, text="Download", command=self.start_download, state="disabled",
                                 padding=(4, 5))
        self.dl_btn.grid(row=0, column=2, sticky="ew")
        self.fw_note = tk.Label(p, text="Only builds Apple still signs can be restored.", bg=T.PANEL, fg=T.QUIET,
                                font=self.fonts.small, anchor="w", justify="left", wraplength=self.px(300))
        self.fw_note.pack(fill="x", padx=16, pady=(6, 0))
        self.dl_row = tk.Frame(p, bg=T.PANEL)
        self.dl_bar = ttk.Progressbar(self.dl_row, mode="determinate", maximum=1.0)
        self.dl_bar.pack(side="left", fill="x", expand=True)
        ttk.Button(self.dl_row, text="Cancel", command=self.cancel_download, padding=(6, 2)).pack(
            side="right", padx=(6, 0))

        self._section(p, "Restore mode")
        self.erase_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(p, text="Update - keep the customer's data", variable=self.erase_var,
                        value=False).pack(anchor="w", padx=16)
        ttk.Radiobutton(p, text="Erase - full restore, deletes all data", variable=self.erase_var,
                        value=True).pack(anchor="w", padx=16, pady=(2, 0))

        self._section(p, "About this phone (optional)")
        tf = tk.Frame(p, bg=T.PANEL)
        tf.pack(fill="x", padx=14)
        tk.Label(tf, text="Ticket", bg=T.PANEL, fg=T.QUIET, font=self.fonts.small).pack(side="left")
        self.ticket_var = tk.StringVar()
        ttk.Entry(tf, textvariable=self.ticket_var, width=14).pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.flag_vars = {}
        grid = tk.Frame(p, bg=T.PANEL)
        grid.pack(fill="x", padx=12, pady=(6, 0))
        for i, flag in enumerate(self.kb.context_flags):
            v = tk.BooleanVar(value=False)
            self.flag_vars[flag["id"]] = v
            ttk.Checkbutton(grid, text=flag["label"], variable=v, command=self._context_changed).grid(
                row=i, column=0, sticky="w")
        dr = tk.Frame(p, bg=T.PANEL)
        dr.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(dr, text="DC supply", bg=T.PANEL, fg=T.QUIET, font=self.fonts.small).pack(side="left")
        self.dc_var = tk.StringVar(value=self.kb.dc_readings[0]["label"])
        dc = ttk.Combobox(dr, textvariable=self.dc_var, state="readonly",
                          values=[r["label"] for r in self.kb.dc_readings])
        dc.pack(side="left", padx=(6, 0), fill="x", expand=True)
        dc.bind("<<ComboboxSelected>>", lambda e: self._context_changed())
        tk.Label(p, text="Notes", bg=T.PANEL, fg=T.QUIET, font=self.fonts.small).pack(anchor="w", padx=16,
                                                                                    pady=(8, 2))
        self.notes_text = tk.Text(p, height=3, bg=T.CARD, fg=T.INK, insertbackground=T.INK, relief="flat",
                                  wrap="word", highlightthickness=1, highlightbackground=T.LINE, padx=6, pady=4)
        self.notes_text.pack(fill="x", padx=14, pady=(0, 12))
        self.notes_text.bind("<FocusOut>", lambda e: self._notes_changed())

    # ------------------------------------------------------------ restore
    def _build_restore_tab(self, p):
        wrap = tk.Frame(p, bg=T.BG)
        wrap.pack(fill="both", expand=True, padx=8, pady=8)
        head = tk.Frame(wrap, bg=T.BG)
        head.pack(fill="x")
        self.now_label = tk.Label(head, text="Ready", bg=T.BG, fg=T.INK, font=self.fonts.h1, anchor="w")
        self.now_label.pack(fill="x")
        self.now_sub = tk.Label(head, text="Check the phone and firmware on the left, then press Start restore.",
                                bg=T.BG, fg=T.QUIET, anchor="w")
        self.now_sub.pack(fill="x", pady=(2, 8))
        self.step_bar = ttk.Progressbar(head, mode="determinate", maximum=1.0)
        self.step_bar.pack(fill="x")

        mid = tk.Frame(wrap, bg=T.BG)
        mid.pack(fill="both", expand=True, pady=(14, 0))
        stages = tk.Frame(mid, bg=T.PANEL, highlightthickness=1, highlightbackground=T.LINE)
        stages.pack(side="left", fill="y")
        tk.Label(stages, text="STAGES", bg=T.PANEL, fg=T.FAINT, font=self.fonts.small).pack(
            anchor="w", padx=14, pady=(12, 4))
        self.stage_rows = {}
        for i, st in enumerate(STAGES, 1):
            r = tk.Frame(stages, bg=T.PANEL)
            r.pack(fill="x", padx=12, pady=5)
            icon = tk.Label(r, text="○", bg=T.PANEL, fg=T.FAINT, font=self.fonts.icon, width=2)
            icon.pack(side="left", anchor="n")
            col = tk.Frame(r, bg=T.PANEL)
            col.pack(side="left", fill="x")
            name = tk.Label(col, text=f"{i}. {STAGE_PLAIN[st]}", bg=T.PANEL, fg=T.QUIET, font=self.fonts.bold,
                            anchor="w")
            name.pack(anchor="w")
            detail = tk.Label(col, text="", bg=T.PANEL, fg=T.FAINT, font=self.fonts.small, anchor="w",
                              width=34, justify="left", wraplength=self.px(250))
            detail.pack(anchor="w")
            self.stage_rows[st] = (icon, name, detail)

        right = tk.Frame(mid, bg=T.BG)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))
        tk.Label(right, text="SIGNALS SO FAR", bg=T.BG, fg=T.FAINT, font=self.fonts.small).pack(anchor="w")
        self.signals = tk.Text(right, height=6, bg=T.CARD, fg=T.INK, relief="flat", wrap="word",
                               font=self.fonts.base, highlightthickness=1, highlightbackground=T.LINE,
                               padx=10, pady=8)
        self.signals.pack(fill="x", pady=(4, 10))
        self.signals.tag_configure("hw", foreground=T.BAD)
        self.signals.tag_configure("setup", foreground=T.WARN)
        self.signals.tag_configure("quiet", foreground=T.QUIET)
        tk.Label(right, text="USB MODE TIMELINE", bg=T.BG, fg=T.FAINT, font=self.fonts.small).pack(anchor="w")
        self.usb_strip = tk.Label(right, text="-", bg=T.CARD, fg=T.INK, anchor="w", justify="left",
                                  wraplength=self.px(600), padx=10, pady=8)
        self.usb_strip.pack(fill="x", pady=(4, 10))
        lh = tk.Frame(right, bg=T.BG)
        lh.pack(fill="x")
        tk.Label(lh, text="ENGINE LOG", bg=T.BG, fg=T.FAINT, font=self.fonts.small).pack(side="left")
        self.show_log = tk.BooleanVar(value=True)
        ttk.Checkbutton(lh, text="Show", variable=self.show_log, command=self._toggle_log,
                        style="TCheckbutton").pack(side="right")
        ttk.Button(lh, text="Save...", command=self.save_log, padding=(8, 2)).pack(side="right", padx=(0, 8))
        ttk.Button(lh, text="Copy", command=self.copy_log, padding=(8, 2)).pack(side="right", padx=(0, 6))
        self.log_frame = tk.Frame(right, bg=T.BG)
        self.log_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.log = tk.Text(self.log_frame, bg=T.MONO_BG, fg="#c9d1dc", relief="flat", wrap="none",
                           font=self.fonts.mono, highlightthickness=1, highlightbackground=T.LINE)
        sb = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("err", foreground="#ff8f8f")
        self.log.tag_configure("dbg", foreground="#6f7a8c")
        # Read-only for typing, but Ctrl+C / Ctrl+A and the arrow keys still work.
        self.log.bind("<Key>", lambda e: None if (e.state & 0x4 and e.keysym.lower() in ("c", "a", "slash"))
                      or e.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End") else "break")

    def _toggle_log(self):
        if self.show_log.get():
            self.log_frame.pack(fill="both", expand=True, pady=(4, 0))
        else:
            self.log_frame.pack_forget()

    def _full_log_text(self) -> str:
        """The whole engine log of the latest attempt (the pane only keeps the last few thousand lines)."""
        if self.session.attempts and self.session.attempts[-1].job is not None:
            try:
                return Path(self.session.attempts[-1].job.raw_log).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return self.log.get("1.0", "end-1c")

    def copy_log(self):
        text = self.log.get("sel.first", "sel.last") if self.log.tag_ranges("sel") else self._full_log_text()
        if not text.strip():
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.now_sub.configure(text="Engine log copied to the clipboard.")

    def save_log(self):
        text = self._full_log_text()
        if not text.strip():
            messagebox.showinfo(APP_NAME, "There is no engine log yet.")
            return
        f = filedialog.asksaveasfilename(title="Save the engine log", defaultextension=".log",
                                         initialfile=f"restore-case{self.session.case_id or ''}.log",
                                         filetypes=[("Log", "*.log"), ("Text", "*.txt")])
        if f:
            Path(f).write_text(text, encoding="utf-8")

    # ------------------------------------------------------------ verdict
    def _build_verdict_tab(self, p):
        self.vscroll = ScrollFrame(p)
        self.vscroll.pack(fill="both", expand=True)
        self._render_verdict(None)

    # ------------------------------------------------------------ history
    def _build_history_tab(self, p):
        bar = tk.Frame(p, bg=T.BG)
        bar.pack(fill="x", padx=8, pady=8)
        tk.Label(bar, text="Search", bg=T.BG, fg=T.QUIET).pack(side="left")
        self.hist_search = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.hist_search, width=30)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda ev: self.refresh_history())
        ttk.Button(bar, text="Search", command=self.refresh_history).pack(side="left")
        ttk.Button(bar, text="Delete", command=self.history_delete).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Open report", command=self.history_open_report).pack(side="right")
        ttk.Button(bar, text="Open folder", command=self.history_open_folder).pack(side="right", padx=6)
        ttk.Button(bar, text="Record fix", command=self.history_record_fix).pack(side="right")
        ttk.Button(bar, text="Show verdict", command=self.history_show).pack(side="right", padx=6)
        tk.Label(bar, text="Double-click a case to see its verdict", bg=T.BG, fg=T.FAINT,
                 font=self.fonts.small).pack(side="left", padx=12)
        cols = ("date", "model", "stop", "verdict", "fix", "n")
        self.hist = ttk.Treeview(p, columns=cols, show="headings", selectmode="browse")
        for c, w, label in (("date", 130, "Date"), ("model", 120, "Phone"), ("stop", 170, "Stopped at"),
                            ("verdict", 330, "Verdict"), ("fix", 150, "Confirmed fix"), ("n", 50, "Runs")):
            self.hist.heading(c, text=label)
            self.hist.column(c, width=self.px(w), minwidth=self.px(40), anchor="w", stretch=(c == "verdict"))
        self.hist.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.hist.bind("<Double-1>", lambda e: self.history_show())
        self.hist.bind("<Delete>", lambda e: self.history_delete())

    # ------------------------------------------------------------- lookup
    def _build_lookup_tab(self, p):
        wrap = tk.Frame(p, bg=T.BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=14)
        tk.Label(wrap, text="Apple error code", bg=T.BG, fg=T.INK, font=self.fonts.h2).pack(anchor="w")
        tk.Label(wrap, text="For restores run in Apple Devices, iTunes, Finder or another tool. A restore run "
                            "here says much more: it shows the step it died on.", bg=T.BG, fg=T.QUIET,
                 wraplength=self.px(760), justify="left").pack(anchor="w", pady=(2, 8))
        row = tk.Frame(wrap, bg=T.BG)
        row.pack(anchor="w")
        self.code_var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.code_var, width=12, font=self.fonts.h2)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.lookup_code())
        ttk.Button(row, text="Look up", command=self.lookup_code).pack(side="left", padx=8)
        ttk.Button(row, text="Import a restore log instead...", command=self.import_log).pack(side="left", padx=8)
        self.lookup_out = tk.Frame(wrap, bg=T.BG)
        self.lookup_out.pack(fill="both", expand=True, pady=(14, 0))

    # =============================================================== checks
    def run_checks(self):
        for w in self.chips.winfo_children():
            w.destroy()
        tk.Label(self.chips, text="Checking...", bg=T.PANEL, fg=T.QUIET).pack(side="left")
        if self.sim:
            from ..engine import CheckResult
            self.q.put(("checks", {"result": [
                CheckResult("engine", "Restore engine", True, "Simulation"),
                CheckResult("amds", "Apple Mobile Device Service", True, "Simulation"),
                CheckResult("driver", "Apple USB driver", True, "Simulation"),
                CheckResult("signing", "Apple signing server", True, "Simulation"),
                CheckResult("hosts", "Hosts file", True, "Simulation"),
                CheckResult("disk", "Free disk space", True, "Simulation")]}))
            return
        self._bg(run_ready_checks, "checks", self.ipsw_folder())

    def _on_checks(self, payload):
        for w in self.chips.winfo_children():
            w.destroy()
        if "error" in payload:
            tk.Label(self.chips, text=f"Checks failed: {payload['error']}", bg=T.PANEL, fg=T.BAD).pack(side="left")
            return
        self.checks = payload["result"]
        self.session.preflight = {c.key: c.as_dict() for c in self.checks}
        short = {"engine": "Engine", "amds": "Apple service", "driver": "Driver", "signing": "Signing",
                 "hosts": "Hosts", "disk": "Disk"}
        for c in self.checks:
            color = T.GOOD if c.ok else (T.QUIET if c.ok is None else T.BAD)
            chip = tk.Label(self.chips, text=f"● {short.get(c.key, c.label)}", bg=T.PANEL, fg=color,
                            font=self.fonts.bold, cursor="hand2", padx=6)
            chip.pack(side="left")
            chip.bind("<Button-1>", lambda e, c=c: self._check_detail(c))

    def _check_detail(self, c):
        msg = f"{c.label}: {'OK' if c.ok else ('not checked' if c.ok is None else 'PROBLEM')}\n\n{c.detail}"
        if c.fix:
            msg += f"\n\nFix: {c.fix}"
        (messagebox.showinfo if c.ok is not False else messagebox.showwarning)(APP_NAME, msg)

    # =============================================================== device
    def probe(self):
        if self.session.running:
            return
        self._probe_pending = False
        self._bg(self.session.probe, "device")

    def _on_device(self, payload):
        if "error" in payload:
            self.dev_meta.configure(text=f"Detect failed: {payload['error']}")
            return
        d: DeviceInfo = payload["result"]
        mode = d.mode if d.connected else "None"
        label = {"None": " NOT CONNECTED ", "Normal": " NORMAL MODE "}.get(mode, f" {mode.upper()} MODE ")
        self.mode_badge.configure(text=label, bg=T.MODE_COLOR.get(mode, T.FAINT))
        if not d.connected:
            self.dev_name.configure(text="Plug in a phone")
            self.dev_meta.configure(text="Put it in DFU or Recovery mode, then press Detect. "
                                         "The badge updates on its own when the USB mode changes.")
            return
        self.dev_name.configure(text=d.name or (d.model.name if d.model else "Unknown iPhone"))
        bits = [d.product or ""]
        if d.ecid:
            bits.append(f"ECID {d.ecid:#x}")
        if d.serial:
            bits.append(f"SN {d.serial}")
        if d.ios:
            bits.append(f"iOS {d.ios}")
        if mode == "Normal":
            bits.append("\nNormal mode works if the phone is unlocked and trusted; Recovery or DFU is more "
                        "reliable.")
        self.dev_meta.configure(text="  ·  ".join(b for b in bits if b))
        if d.product:
            self.load_builds()

    def _on_usb(self, payload):
        mode = payload["mode"]
        count = getattr(self.usb, "phone_count", 0)
        if count > 1:
            self.usb_label.configure(text=f"USB: {count} phones connected - unplug the others", fg=T.BAD)
        else:
            self.usb_label.configure(text=f"USB: {mode}", fg=T.MODE_COLOR.get(mode, T.QUIET))
        self._update_dfu_status(mode)
        if self.session.running:
            self._refresh_usb_strip()
        elif not self._probe_pending:
            self._probe_pending = True
            self.after(1500, self.probe)

    def _refresh_usb_strip(self):
        if not self.session.attempts:
            return
        att = self.session.attempts[-1]
        tl = self.usb.since(att.started_mono)
        if not tl:
            self.usb_strip.configure(text="-")
            return
        parts = [f"{mode} @{int(t)}s" for t, mode in tl[-12:]]
        self.usb_strip.configure(text="  →  ".join(parts))

    def show_dfu_helper(self):
        if getattr(self, "_dfu_win", None) and self._dfu_win.winfo_exists():
            self._dfu_win.lift()
            return
        model = self.session.device.model
        w = tk.Toplevel(self)
        w.title("DFU / Recovery helper")
        w.configure(bg=T.PANEL)
        w.transient(self)
        self._dfu_win = w
        name = model.name if model else "this iPhone"
        tk.Label(w, text=f"DFU mode on {name}", bg=T.PANEL, fg=T.INK, font=self.fonts.h2).pack(
            anchor="w", padx=18, pady=(16, 6))
        for i, step in enumerate(dfu_steps(model), 1):
            tk.Label(w, text=f"{i}. {step}", bg=T.PANEL, fg=T.INK, wraplength=self.px(460),
                     justify="left").pack(anchor="w", padx=22, pady=1)
        tk.Label(w, text="Recovery mode instead", bg=T.PANEL, fg=T.QUIET, font=self.fonts.h3).pack(
            anchor="w", padx=18, pady=(14, 4))
        for i, step in enumerate(recovery_steps(model), 1):
            tk.Label(w, text=f"{i}. {step}", bg=T.PANEL, fg=T.QUIET, wraplength=self.px(460),
                     justify="left").pack(anchor="w", padx=22, pady=1)
        tk.Label(w, text="If no model is detected yet, the steps for iPhone 8 and later are shown.",
                 bg=T.PANEL, fg=T.FAINT, font=self.fonts.small).pack(anchor="w", padx=18, pady=(10, 0))
        self._dfu_status = tk.Label(w, text="", bg=T.CARD, fg=T.INK, font=self.fonts.h3, padx=12, pady=10,
                                    anchor="w", justify="left", wraplength=self.px(460))
        self._dfu_status.pack(fill="x", padx=18, pady=(12, 16))
        self._update_dfu_status(self.usb.current or self.session.device.mode)

    def _update_dfu_status(self, mode: str | None):
        lab = getattr(self, "_dfu_status", None)
        if not lab or not lab.winfo_exists():
            return
        text = {
            "DFU": ("Phone is in DFU mode. You can close this and start the restore.", T.GOOD),
            "Recovery": ("Phone is in Recovery mode (fine for a restore). For DFU, start the steps again.", T.WARN),
            "Normal": ("Phone is on in normal mode. Follow the steps above.", T.QUIET),
        }.get(mode or "", ("No phone on USB yet. Plug it in and follow the steps.", T.QUIET))
        lab.configure(text=text[0], fg=text[1])

    # ============================================================= firmware
    def ipsw_folder(self) -> Path:
        return Path(self.settings.get("ipsw_folder") or paths.ipsw_dir())

    def choose_ipsw_folder(self):
        d = filedialog.askdirectory(title="Folder with IPSW files", initialdir=str(self.ipsw_folder()))
        if d:
            self.settings["ipsw_folder"] = d
            save_settings(self.settings)
            self.load_builds()
            self.run_checks()  # the disk check covers the IPSW folder's drive

    def load_builds(self):
        product = self.session.device.product or ("iPhone14,5" if self.sim else None)
        if not product:
            self.fw_note.configure(text="Detect the phone first so the right firmware can be listed.")
            return
        self.fw_note.configure(text="Checking which builds Apple signs...")
        folder = self.ipsw_folder()
        sim = self.sim

        def work():
            local = local_builds(folder, product)
            if sim:
                return merge([Build(product, "26.0.1", "23A355", True, url=None, path=Path("simulated.ipsw"))],
                             local), None
            try:
                return merge(signed_builds(product), local), None
            except StaleList as stale:
                return merge(stale.builds, local), str(stale)
            except Exception as exc:  # noqa: BLE001
                return local, f"Could not reach api.ipsw.me ({exc}); showing local files only."
        self._bg(work, "builds")

    def _on_builds(self, payload):
        if "error" in payload:
            self.fw_note.configure(text=payload["error"])
            return
        builds, note = payload["result"]
        extra = [b for b in self.builds if b.path and b.path.suffix == ".ipsw" and b not in builds
                 and b.product == self.session.device.product and b.path.name != "simulated.ipsw"]
        self.builds = builds + extra
        labels = [self._build_label(b) for b in self.builds]
        self.fw_combo.configure(values=labels)
        if labels and not self.fw_var.get() in labels:
            self.fw_var.set(labels[0])
        self._fw_selected()
        if note:
            self.fw_note.configure(text=note)
        elif not self.builds:
            self.fw_note.configure(text="No signed builds found for this phone.")

    def _build_label(self, b: Build) -> str:
        tag = "signed" if b.signed else "not signed?"
        disk = "on disk" if b.path else "download"
        return f"iOS {b.version} ({b.build}) - {tag}, {disk}"

    def selected_build(self) -> Build | None:
        label = self.fw_var.get()
        for b in self.builds:
            if self._build_label(b) == label:
                return b
        return None

    def _fw_selected(self):
        b = self.selected_build()
        if not b:
            self.dl_btn.configure(state="disabled")
            return
        can_dl = b.url and not b.path and not self._downloading.is_set()
        self.dl_btn.configure(state="normal" if can_dl else "disabled")
        if b.path:
            self.fw_note.configure(text=f"Using {b.path.name}" + ("" if b.signed else
                                   "  (not in Apple's signed list: the restore will fail at signing)"))
        else:
            size = f" ({b.size / 1e9:.1f} GB)" if b.size else ""
            self.fw_note.configure(text=f"Not downloaded yet{size}. Press Download, or Start downloads it first.")

    def browse_ipsw(self):
        f = filedialog.askopenfilename(title="Choose an IPSW", filetypes=[("IPSW firmware", "*.ipsw")],
                                       initialdir=str(self.ipsw_folder()))
        if not f:
            return
        info = read_ipsw(Path(f))
        if not info:
            messagebox.showerror(APP_NAME, "That file is not a readable IPSW.")
            return
        product = self.session.device.product
        if product and product not in info["products"]:
            if not messagebox.askyesno(APP_NAME, f"This IPSW does not list {product}. Use it anyway?"):
                return
        b = Build(product or info["products"][0], info["version"], info["build"],
                  signed=any(x.signed and x.build == info["build"] for x in self.builds), path=Path(f))
        self.builds.append(b)
        labels = [self._build_label(x) for x in self.builds]
        self.fw_combo.configure(values=labels)
        self.fw_var.set(self._build_label(b))
        self._fw_selected()

    def start_download(self, then_start: bool = False):
        b = self.selected_build()
        if not b or not b.url or self._downloading.is_set():
            return
        self._downloading.set()
        self._cancel_download = False
        self.dl_row.pack(fill="x", padx=16, pady=(6, 0), after=self.fw_note)
        self.dl_bar["value"] = 0
        self.dl_btn.configure(state="disabled")
        folder = self.ipsw_folder()

        def work():
            path = download(b, folder, lambda f, msg: self.q.put(("dl_progress", {"f": f, "msg": msg})),
                            lambda: self._cancel_download)
            return b, path, then_start
        self._bg(work, "dl_done")

    def _on_dl_progress(self, p):
        self.dl_bar["value"] = p["f"]
        self.fw_note.configure(text=f"Downloading... {p['msg']}")

    def cancel_download(self):
        if self._downloading.is_set():
            self._cancel_download = True
            self.fw_note.configure(text="Cancelling the download...")

    def _on_dl_done(self, payload):
        self._downloading.clear()
        self.dl_row.pack_forget()
        if "error" in payload:
            self._fw_selected()
            if self._cancel_download:
                self.fw_note.configure(text="Download cancelled. Press Download to resume where it stopped.")
            else:
                self.fw_note.configure(text=f"Download failed: {payload['error']}")
            return
        b, path, then_start = payload["result"]
        b.path = path
        labels = [self._build_label(x) for x in self.builds]
        self.fw_combo.configure(values=labels)
        self.fw_var.set(self._build_label(b))
        self._fw_selected()
        if then_start:
            self.start()

    # =============================================================== restore
    def _context_changed(self):
        s = self.session
        s.context = {k for k, v in self.flag_vars.items() if v.get()}
        label = self.dc_var.get()
        s.dc_reading = next((r["id"] for r in self.kb.dc_readings if r["label"] == label), None)
        if s.dc_reading == "none":
            s.dc_reading = None
        if s.attempts and not s.running and s.verdict is not None:
            self._render_verdict(s.rediagnose())

    def _notes_changed(self):
        s = self.session
        s.notes = self.notes_text.get("1.0", "end").strip()
        s.ticket = self.ticket_var.get().strip()
        if s.case_id is not None:
            s.store.set_context(s.case_id, s.ticket, s.notes, s.context, s.dc_reading)

    def start(self):
        s = self.session
        if s.running:
            return
        if getattr(self.usb, "phone_count", 0) > 1 and not self.sim:
            if not messagebox.askyesno(APP_NAME, f"{self.usb.phone_count} Apple phones are on USB. The restore "
                                                 "and the USB watch may pick different phones.\n\nUnplug the "
                                                 "others first. Start anyway?", icon="warning"):
                return
        if not s.device.connected and not self.sim:
            if not messagebox.askyesno(APP_NAME, "No phone is detected. Start anyway? (The engine waits a few "
                                                 "seconds for one to appear.)"):
                return
        b = self.selected_build()
        if b is None:
            messagebox.showinfo(APP_NAME, "Choose a firmware first (Detect the phone to list its builds, or "
                                          "Browse to an IPSW).")
            return
        if not b.path:
            if messagebox.askyesno(APP_NAME, f"iOS {b.version} isn't downloaded yet. Download it now and start "
                                             "when it's done?"):
                self.start_download(then_start=True)
            return
        erase = self.erase_var.get()
        if erase and not messagebox.askyesno(
                APP_NAME, "ERASE restore: every photo, message and app on this phone will be deleted for good."
                          "\n\nContinue only if the customer has agreed or has a backup.", icon="warning"):
            return
        if erase:
            typed = simpledialog.askstring(APP_NAME, "Type ERASE to confirm wiping this phone:", parent=self)
            if (typed or "").strip().upper() != "ERASE":
                return
        if not erase and s.device.ios and b.version and self._newer(s.device.ios, b.version):
            if not messagebox.askyesno(APP_NAME, f"The phone runs iOS {s.device.ios}, newer than {b.version}. "
                                                 "An Update cannot downgrade and may fail or lose data.\n\n"
                                                 "Continue?", icon="warning"):
                return
        bad = [c for c in self.checks if c.ok is False and c.key in ("engine", "amds", "signing", "hosts")]
        if bad and not self.sim:
            names = ", ".join(c.label for c in bad)
            if not messagebox.askyesno(APP_NAME, f"The ready check found problems ({names}). The restore will "
                                                 "probably fail for that reason, not the phone's.\n\nStart anyway?"):
                return
        s.ticket = self.ticket_var.get().strip()
        s.notes = self.notes_text.get("1.0", "end").strip()
        self._context_changed()
        try:
            s.start_attempt(b.path, erase)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Could not start the engine:\n{exc}")

    @staticmethod
    def _newer(a: str, b: str) -> bool:
        def key(v):
            parts = [int(x) if x.isdigit() else 0 for x in v.split(".")]
            return parts + [0] * (4 - len(parts))  # "17" and "17.0" are the same version
        return key(a) > key(b)

    def cancel(self):
        if self.session.running and messagebox.askyesno(
                APP_NAME, "Stop the restore now? The phone will be left in Recovery or DFU mode and will need "
                          "another restore.", icon="warning"):
            self.session.cancel()

    def new_phone(self):
        if self.session.running:
            return
        self.session.new_case()
        self._viewing_case = None
        for v in self.flag_vars.values():
            v.set(False)
        self.dc_var.set(self.kb.dc_readings[0]["label"])
        self.ticket_var.set("")
        self.notes_text.delete("1.0", "end")
        self.erase_var.set(False)
        self._reset_tracker()
        self.now_label.configure(text="Ready")
        self.now_sub.configure(text="Check the phone and firmware on the left, then press Start restore.")
        self._render_verdict(None)
        self.nb.select(self.tab_restore)
        self.probe()

    def _reset_tracker(self):
        for st, (icon, name, detail) in self.stage_rows.items():
            icon.configure(text="○", fg=T.FAINT)
            name.configure(fg=T.QUIET)
            detail.configure(text="", fg=T.FAINT)
        self.step_bar["value"] = 0
        self.signals.delete("1.0", "end")
        self._signal_counts = {}
        self.log.delete("1.0", "end")
        self.usb_strip.configure(text="-")

    def _set_controls(self, running: bool):
        self.start_btn.configure(state="disabled" if running else "normal",
                                 text="Restoring..." if running else
                                 ("Run again" if self.session.attempts else "Start restore"))
        self.cancel_btn.configure(state="normal" if running else "disabled")

    def _on_attempt_started(self, p):
        self._reset_tracker()
        self._set_controls(True)
        self.nb.select(self.tab_restore)
        n = p["attempt"]
        self.now_label.configure(text=f"Attempt {n}: starting")
        self.now_sub.configure(text=("Erase restore" if p["erase"] else "Update restore (keeps data)")
                               + f" with {Path(p['ipsw']).name}")
        self._active_stage = None

    def _mark_stage(self, stage: str, failed: bool = False, all_done: bool = False):
        idx = STAGES.index(stage)
        summary = self.session.attempts[-1].summary if self.session.attempts else None
        top = STAGES.index(summary.max_stage) if summary else idx
        for i, st in enumerate(STAGES):
            icon, name, detail = self.stage_rows[st]
            if all_done or (i < idx) or (i <= top and i != idx):
                icon.configure(text="✓", fg=T.GOOD)
                name.configure(fg=T.INK)
            elif i == idx:
                icon.configure(text="✕" if failed else "●", fg=T.BAD if failed else T.ACCENT)
                name.configure(fg=T.INK)
            else:
                icon.configure(text="○", fg=T.FAINT)
                name.configure(fg=T.QUIET)

    def _on_events(self, p):
        for ev in p["events"]:
            k, d = ev.kind, ev.data
            if k == "stage":
                self._mark_stage(d["stage"])
                self.now_label.configure(text=d["plain"])
            elif k == "step":
                _, _, detail = self.stage_rows[d["stage"]]
                detail.configure(text=f"{d['plain']}  ({d['name']})", fg=T.QUIET)
                self.now_label.configure(text=d["plain"])
                self.step_bar["value"] = 0
            elif k == "activity":
                _, _, detail = self.stage_rows.get(d["stage"], (None, None, None))
                if detail:
                    detail.configure(text=d["plain"], fg=T.QUIET)
                self.now_label.configure(text=d["plain"])
            elif k == "progress":
                f = d["fraction"]
                self.step_bar["value"] = f
                if d.get("plain") and d["step"] in (2, 3):
                    self.now_label.configure(text=f"{d['plain']}  {int(f * 100)}%")
            elif k == "mode":
                self._refresh_usb_strip()
            elif k == "signature":
                tag = {"hardware": "hw", "setup": "setup", "firmware": "setup"}.get(d["sig_kind"], "quiet")
                # one line per signal; repeats bump a counter instead of filling the box
                n = self._signal_counts.get(d["title"], 0) + 1
                self._signal_counts[d["title"]] = n
                if n == 1:
                    self.signals.insert("end", f"• {d['title']}\n", tag)
                else:
                    line = self.signals.search(f"• {d['title']}", "1.0", stopindex="end")
                    if line:
                        self.signals.delete(line, f"{line} lineend")
                        self.signals.insert(line, f"• {d['title']}  ×{n}", tag)
                self.signals.see("end")
            elif k == "device":
                if d.get("product") and not self.session.device.product:
                    self.session.device.product = d["product"]
            elif k == "restore_os_up":
                self.now_sub.configure(text="The phone's restore OS is up and reporting its own steps.")
            elif k == "panic_log":
                self.signals.insert("end", "• The phone handed over a panic from its previous crash\n", "hw")
            elif k == "outcome":
                pass

    def _on_line(self, p):
        line = p["line"]
        if not line or line.startswith("progress:"):
            return
        tag = "err" if "<Error>" in line or line.startswith("ERROR") else ("dbg" if "<Debug>" in line else "")
        self.log.insert("end", line + "\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 4000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")

    def _on_settling(self, p):
        self.now_sub.configure(text=f"Watching where the phone goes next (up to {int(p['seconds'])} s)...")

    def _on_attempt_aborted(self, p):
        self._set_controls(False)
        self.now_label.configure(text="The engine did not start")
        self.now_sub.configure(text=p.get("error") or "")

    def _on_attempt_finished(self, p):
        s = p["summary"]
        v: Verdict | None = p["verdict"]
        self._set_controls(False)
        if p.get("error") or v is None:
            self._mark_stage(s.stop_stage or s.stage, failed=s.outcome != "success")
            self.now_label.configure(text="Restore ended")
            self.now_sub.configure(text=f"Could not build the verdict: {p.get('error') or 'unknown error'}. "
                                        "The raw log is kept in the case folder.")
            self.refresh_history()
            return
        if s.outcome == "success":
            self._mark_stage("finish", all_done=True)
            self.now_label.configure(text="Restore finished")
        else:
            self._mark_stage(s.stop_stage or s.stage, failed=True)
            self.now_label.configure(text="Restore stopped" if s.outcome == "cancelled" else "Restore failed")
        self.now_sub.configure(text=v.headline)
        self._refresh_usb_strip()
        self._viewing_case = self.session.case_id
        self._render_verdict(v)
        self.nb.select(self.tab_verdict)
        self.refresh_history()

    # =============================================================== verdict
    def _render_verdict(self, v: Verdict | None, case_row=None, similar=None, imported: str | None = None):
        sf = self.vscroll
        sf.clear()
        b = sf.body
        if v is None:
            tk.Label(b, text="No verdict yet", bg=T.BG, fg=T.INK, font=self.fonts.h1).pack(anchor="w", padx=18,
                                                                                         pady=(18, 4))
            tk.Label(b, text="Run a restore, open a case from History, or import a log.", bg=T.BG,
                     fg=T.QUIET).pack(anchor="w", padx=18)
            return
        color = T.CATEGORY_COLOR.get(v.category, T.ACCENT)
        head = tk.Frame(b, bg=T.CARD, highlightthickness=0)
        head.pack(fill="x", padx=14, pady=(14, 8))
        tk.Frame(head, bg=color, width=6).pack(side="left", fill="y")
        hb = tk.Frame(head, bg=T.CARD)
        hb.pack(side="left", fill="both", expand=True, padx=14, pady=12)
        if case_row is not None:
            tk.Label(hb, text=f"Case {case_row['id']} · {case_row['created']} · "
                              f"{case_row['model_name'] or case_row['product'] or ''}",
                     bg=T.CARD, fg=T.QUIET, font=self.fonts.small).pack(anchor="w")
        elif imported:
            tk.Label(hb, text=f"Imported log: {imported} (no USB watch data, so PC-side causes can't be ruled out)",
                     bg=T.CARD, fg=T.QUIET, font=self.fonts.small).pack(anchor="w")
        tk.Label(hb, text=v.headline, bg=T.CARD, fg=T.INK, font=self.fonts.h1, wraplength=820, justify="left",
                 anchor="w").pack(fill="x")
        pills = tk.Frame(hb, bg=T.CARD)
        pills.pack(anchor="w", pady=(6, 4))
        for txt, fg in ((v.category.upper(), color), (f"confidence: {v.confidence}", T.INK),
                        (f"{v.attempts} attempt{'s' if v.attempts != 1 else ''}", T.QUIET)) + \
                (((" same stop repeated ", T.BAD),) if v.repeated else ()):
            tk.Label(pills, text=f" {txt} ", bg=T.CARD_HI, fg=fg, font=self.fonts.small).pack(side="left",
                                                                                           padx=(0, 6))
        if v.summary:
            tk.Label(hb, text=v.summary, bg=T.CARD, fg=T.QUIET, wraplength=820, justify="left",
                     anchor="w").pack(fill="x", pady=(4, 0))

        btns = tk.Frame(b, bg=T.BG)
        btns.pack(fill="x", padx=14, pady=(0, 6))
        live = self._viewing_case is not None and self._viewing_case == self.session.case_id and case_row is None
        if live and v.category not in ("success",):
            ttk.Button(btns, text="Run again", style="Accent.TButton", command=self._run_again).pack(side="left")
        if not imported:
            ttk.Button(btns, text="Open report", command=self._open_current_report).pack(side="left", padx=6)
            ttk.Button(btns, text="Record the fix", command=self._record_fix_current).pack(side="left")
        if live:
            ttk.Button(btns, text="New phone", command=self.new_phone).pack(side="right")

        if v.next_checks:
            self._vsection(b, "Next checks")
            box = tk.Frame(b, bg=T.CARD)
            box.pack(fill="x", padx=14)
            # Ticks are saved with the case, so re-rendering (a context change, reopening it) keeps them.
            check_case = case_row["id"] if case_row is not None else (self.session.case_id if live else None)
            done = self._checks_done(check_case)
            for i, c in enumerate(v.next_checks, 1):
                var = tk.BooleanVar(value=c in done)
                r = tk.Frame(box, bg=T.CARD)
                r.pack(fill="x", padx=10, pady=3)
                tk.Checkbutton(r, variable=var, bg=T.CARD, activebackground=T.CARD, selectcolor=T.PANEL,
                               highlightthickness=0, bd=0, state="normal" if check_case else "disabled",
                               command=lambda c=c, var=var: self._tick_check(check_case, c, var.get())
                               ).pack(side="left", anchor="n")
                tk.Label(r, text=f"{i}. {c}", bg=T.CARD, fg=T.INK, wraplength=800, justify="left",
                         anchor="w").pack(side="left", fill="x")

        if v.suspects:
            self._vsection(b, "Suspects, most likely first")
            for s in v.suspects:
                card = tk.Frame(b, bg=T.CARD, highlightthickness=1, highlightbackground=T.LINE)
                card.pack(fill="x", padx=14, pady=4)
                top = tk.Frame(card, bg=T.CARD)
                top.pack(fill="x", padx=12, pady=(10, 2))
                kind_col = {"hardware": T.BAD, "setup": T.WARN, "firmware": T.WARN}.get(s.kind, T.QUIET)
                tk.Label(top, text=s.label, bg=T.CARD, fg=T.INK, font=self.fonts.h3, wraplength=560,
                         justify="left").pack(side="left")
                tk.Label(top, text=f"  {s.kind}", bg=T.CARD, fg=kind_col, font=self.fonts.small).pack(side="left")
                meter = tk.Canvas(top, width=160, height=10, bg=T.PANEL, highlightthickness=0)
                meter.pack(side="right", pady=4)
                meter.create_rectangle(0, 0, int(160 * min(s.score, 1.0)), 10, fill=kind_col, width=0)
                tk.Label(top, text=f"{s.percent}%  ", bg=T.CARD, fg=T.QUIET, font=self.fonts.small).pack(
                    side="right")
                for r in s.reasons[:5]:
                    tk.Label(card, text=f"• {r}", bg=T.CARD, fg=T.INK, wraplength=820, justify="left",
                             anchor="w").pack(fill="x", padx=16)
                for n in s.notes:
                    tk.Label(card, text=f"Note: {n}", bg=T.CARD, fg=T.WARN, wraplength=820, justify="left",
                             anchor="w").pack(fill="x", padx=16, pady=(2, 0))
                for e in s.evidence[:3]:
                    tk.Label(card, text=e, bg=T.MONO_BG, fg="#c9d1dc", font=self.fonts.mono, wraplength=820,
                             justify="left", anchor="w", padx=8, pady=3).pack(fill="x", padx=16, pady=(4, 0))
                tk.Frame(card, bg=T.CARD, height=10).pack()

        if v.panic and not v.panic.empty:
            self._vsection(b, "Panic from an earlier crash")
            pc = tk.Frame(b, bg=T.CARD)
            pc.pack(fill="x", padx=14)
            tk.Label(pc, text=v.panic.panic_string, bg=T.MONO_BG, fg="#ffb4b4", font=self.fonts.mono,
                     wraplength=820, justify="left", anchor="w", padx=8, pady=6).pack(fill="x", padx=10, pady=8)
            if v.panic.missing_sensors:
                tk.Label(pc, text="Missing sensors: " + ", ".join(v.panic.missing_sensors) +
                         "  (sensor names are model-specific; check them in the BenchTool iOS Panic Diagnoser)",
                         bg=T.CARD, fg=T.INK, wraplength=820, justify="left").pack(anchor="w", padx=10,
                                                                                   pady=(0, 8))

        if v.warnings:
            self._vsection(b, "Notes")
            for w in v.warnings:
                tk.Label(b, text=f"• {w}", bg=T.BG, fg=T.QUIET, wraplength=840, justify="left",
                         anchor="w").pack(fill="x", padx=18)

        sim = similar if similar is not None else (self.session.similar_cases() if live else [])
        if sim:
            self._vsection(b, "Earlier cases of this model that stopped here")
            for r in sim:
                tk.Label(b, text=f"• {r['created']}: fixed by {self.kb.part_label(r['fix_part'])}"
                                 f"{' - ' + r['fix_notes'] if r.get('fix_notes') else ''}",
                         bg=T.BG, fg=T.INK, wraplength=840, justify="left", anchor="w").pack(fill="x", padx=18)

        stop = v.stop or {}
        self._vsection(b, "Where it stopped")
        where = []
        if stop.get("stage_no"):
            where.append(f"Stage {stop['stage_no']} of 7: {stop.get('stage_plain')}")
        if stop.get("step_plain"):
            where.append(f"Step: {stop['step_plain']} ({stop.get('step')})")
        if stop.get("activity"):
            where.append(f"Activity: {stop['activity']}")
        if stop.get("progress") is not None:
            where.append(f"Progress: {int(stop['progress'] * 100)}%")
        where.append("Restore OS came up: " + ("yes" if stop.get("restore_os_up") else "no"))
        tk.Label(b, text="\n".join(where), bg=T.BG, fg=T.QUIET, justify="left", anchor="w").pack(
            fill="x", padx=18, pady=(0, 18))
        self._fit_wraps(sf)

    def _fit_wraps(self, sf: ScrollFrame) -> None:
        """The labels above are laid out for a 900 px page; make their wrap width follow the real
        width instead, so text fills a wide window and still fits at high DPI."""
        def walk(w):
            for child in w.winfo_children():
                if isinstance(child, tk.Label):
                    wl = int(float(str(child.cget("wraplength")) or 0))
                    if wl:
                        sf.wrap(child, self.px(900 - wl))
                walk(child)
        walk(sf.body)

    def _checks_done(self, case_id: int | None) -> set:
        if case_id is None:
            return set()
        row = self.store.get(case_id)
        try:
            return set(json.loads(row["checks_done"] or "[]")) if row else set()
        except ValueError:
            return set()

    def _tick_check(self, case_id: int | None, check: str, on: bool) -> None:
        if case_id is None:
            return
        done = self._checks_done(case_id)
        (done.add if on else done.discard)(check)
        self.store.set_checks_done(case_id, sorted(done))

    def _vsection(self, parent, title):
        tk.Label(parent, text=title.upper(), bg=T.BG, fg=T.FAINT, font=self.fonts.small).pack(
            anchor="w", padx=16, pady=(14, 4))

    def _run_again(self):
        messagebox.showinfo(APP_NAME, "Before running again, move the phone to a different cable and a rear USB "
                                      "port. If it stops at the same point, the result is confirmed.")
        self.start()

    def _open_current_report(self):
        cid = self._viewing_case or self.session.case_id
        if cid is None:
            return
        row = self.store.get(cid)
        path = Path(row["folder"]) / "report.html"
        if cid == self.session.case_id:
            p = self.session.write_report()
            path = p or path
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo(APP_NAME, "No report yet for this case.")

    def _record_fix_current(self):
        cid = self._viewing_case or self.session.case_id
        if cid is not None:
            self._record_fix_dialog(cid)

    def _record_fix_dialog(self, case_id: int):
        row = self.store.get(case_id)
        dlg = tk.Toplevel(self)
        dlg.title("Record the fix")
        dlg.configure(bg=T.PANEL)
        dlg.transient(self)
        tk.Label(dlg, text="What fixed this phone?", bg=T.PANEL, fg=T.INK, font=self.fonts.h2).pack(
            anchor="w", padx=16, pady=(14, 4))
        tk.Label(dlg, text="Saved with the case so later verdicts for this model can show what worked.",
                 bg=T.PANEL, fg=T.QUIET, wraplength=420, justify="left").pack(anchor="w", padx=16)
        parts = [(k, v["label"]) for k, v in self.kb.components.items()]
        var = tk.StringVar(value=self.kb.part_label(row["fix_part"]) if row["fix_part"] else "")
        cb = ttk.Combobox(dlg, textvariable=var, values=[label for _, label in parts], state="readonly", width=56)
        cb.pack(fill="x", padx=16, pady=(10, 6))
        tk.Label(dlg, text="Notes (what you replaced or reworked)", bg=T.PANEL, fg=T.QUIET).pack(anchor="w",
                                                                                               padx=16)
        notes = tk.Text(dlg, height=4, bg=T.CARD, fg=T.INK, insertbackground=T.INK, relief="flat", width=56)
        notes.insert("1.0", row["fix_notes"] or "")
        notes.pack(fill="x", padx=16, pady=(4, 10))

        def save():
            key = next((k for k, label in parts if label == var.get()), None)
            if not key:
                messagebox.showinfo(APP_NAME, "Choose the part that fixed it.", parent=dlg)
                return
            self.store.record_fix(case_id, key, notes.get("1.0", "end").strip())
            dlg.destroy()
            self.refresh_history()
        fr = tk.Frame(dlg, bg=T.PANEL)
        fr.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Button(fr, text="Save", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(fr, text="Cancel", command=dlg.destroy).pack(side="right", padx=8)

    # =============================================================== history
    def refresh_history(self):
        for i in self.hist.get_children():
            self.hist.delete(i)
        for c in self.store.list(self.hist_search.get().strip()):
            stop = c.stop_step or ""
            op = self.kb.operation(stop) if stop else None
            self.hist.insert("", "end", iid=str(c.id), values=(
                c.created, c.model_name or c.product, op["plain"] if op else (stop or "-"),
                c.headline, self.kb.part_label(c.fix_part) if c.fix_part else "", c.attempts))

    def _hist_selected(self) -> int | None:
        sel = self.hist.selection()
        return int(sel[0]) if sel else None

    def history_show(self):
        cid = self._hist_selected()
        if cid is None:
            return
        row = self.store.get(cid)
        attempts, usb = self.store.reload_attempts(cid)
        if not attempts:
            messagebox.showinfo(APP_NAME, "This case has no recorded attempts.")
            return
        from ..diagnose import observe_usb
        from ..models import find_by_product
        ctx = set(json.loads(row["context"] or "[]"))
        # the same ready-check result the live verdict used, so the two agree
        preflight = json.loads(row["preflight"] or "{}") if "preflight" in row.keys() else {}
        v = diagnose(attempts, find_by_product(row["product"]) if row["product"] else None,
                     usb=[observe_usb(u, a) if u is not None else None for a, u in zip(attempts, usb)],
                     context=ctx, dc_reading=row["dc_reading"], preflight=preflight)
        sim = [dict(r) for r in self.store.similar(row["product"], v.stop.get("stage"), v.stop.get("step"),
                                                    exclude=cid)]
        self._viewing_case = cid
        self._render_verdict(v, case_row=row, similar=sim)
        self.nb.select(self.tab_verdict)

    def history_open_folder(self):
        cid = self._hist_selected()
        if cid is not None:
            open_path(Path(self.store.get(cid)["folder"]))

    def history_open_report(self):
        cid = self._hist_selected()
        if cid is None:
            return
        path = Path(self.store.get(cid)["folder"]) / "report.html"
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo(APP_NAME, "No report for this case.")

    def history_delete(self):
        cid = self._hist_selected()
        if cid is None:
            return
        if cid == self.session.case_id and self.session.running:
            messagebox.showinfo(APP_NAME, "This case has a restore running. Stop it first.")
            return
        row = self.store.get(cid)
        name = row["model_name"] or row["product"] or "unknown phone"
        if not messagebox.askyesno(APP_NAME, f"Delete case {cid} ({name}, {row['created']})?\n\nIts logs and "
                                             "report are deleted too. This can't be undone.", icon="warning"):
            return
        self.store.delete(cid)
        if cid == self.session.case_id:
            self.new_phone()
        elif self._viewing_case == cid:
            self._viewing_case = None
            self._render_verdict(None)
        self.refresh_history()

    def history_record_fix(self):
        cid = self._hist_selected()
        if cid is not None:
            self._record_fix_dialog(cid)

    # ================================================================ lookup
    def lookup_code(self):
        for w in self.lookup_out.winfo_children():
            w.destroy()
        txt = self.code_var.get().strip()
        try:
            code = int(txt)
        except ValueError:
            tk.Label(self.lookup_out, text="Type the number Apple showed, e.g. 4013.", bg=T.BG, fg=T.QUIET).pack(
                anchor="w")
            return
        entry = self.kb.lookup_apple_code(code)
        if not entry:
            tk.Label(self.lookup_out, text=f"No notes for error {code} yet. Run the restore here to see the "
                                           "step it fails on.", bg=T.BG, fg=T.QUIET).pack(anchor="w")
            return
        card = tk.Frame(self.lookup_out, bg=T.CARD)
        card.pack(fill="x")
        color = T.CATEGORY_COLOR.get(entry["kind"], T.ACCENT)
        tk.Frame(card, bg=color, width=6).pack(side="left", fill="y")
        body = tk.Frame(card, bg=T.CARD)
        body.pack(side="left", fill="both", expand=True, padx=14, pady=12)
        tk.Label(body, text=f"Error {code}", bg=T.CARD, fg=T.INK, font=self.fonts.h1).pack(anchor="w")
        tk.Label(body, text=entry["meaning"], bg=T.CARD, fg=T.INK, wraplength=820, justify="left").pack(
            anchor="w", pady=(4, 8))
        tk.Label(body, text="USUAL SUSPECTS", bg=T.CARD, fg=T.FAINT, font=self.fonts.small).pack(anchor="w")
        for p in sorted(entry["parts"], key=lambda x: -x["w"]):
            tk.Label(body, text=f"• {self.kb.part_label(p['part'])}", bg=T.CARD, fg=T.INK).pack(anchor="w")
        if entry.get("notes"):
            tk.Label(body, text="NOTES", bg=T.CARD, fg=T.FAINT, font=self.fonts.small).pack(anchor="w",
                                                                                          pady=(8, 0))
            for n in entry["notes"]:
                tk.Label(body, text=f"• {n}", bg=T.CARD, fg=T.QUIET, wraplength=820, justify="left").pack(
                    anchor="w")
        if entry.get("sources"):
            tk.Label(body, text="SOURCES", bg=T.CARD, fg=T.FAINT, font=self.fonts.small).pack(anchor="w",
                                                                                            pady=(8, 0))
            for u in entry["sources"]:
                lnk = tk.Label(body, text=u, bg=T.CARD, fg=T.ACCENT, cursor="hand2")
                lnk.pack(anchor="w")
                lnk.bind("<Button-1>", lambda e, u=u: webbrowser.open(u))

    def import_log(self):
        f = filedialog.askopenfilename(title="Restore log to analyse",
                                       filetypes=[("Logs", "*.log *.txt"), ("All files", "*.*")])
        if not f:
            return
        p = RestoreLogParser(clock=lambda: 0.0)
        text = Path(f).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            p.feed(line, "out" if line.startswith("progress:") else "err")
        # A log that never says DONE is treated as a failed run.
        a = p.finish(0 if p.summary().outcome == "success" else 1)
        from ..models import find_by_product
        product = a.device.get("product")
        v = diagnose([a], find_by_product(product) if product else None)
        self._viewing_case = None
        self._render_verdict(v, imported=Path(f).name)
        self.nb.select(self.tab_verdict)


def _windows_dpi_aware() -> None:
    """Without this, Windows bitmap-stretches the window on scaled displays (blurry)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
    except Exception:  # noqa: BLE001 - older Windows: stay unaware
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    _windows_dpi_aware()
    sim = paths.simulation_enabled() or "--sim" in argv
    scenario = None
    for a in argv:
        if a.startswith("--scenario="):
            scenario, sim = a.split("=", 1)[1], True
    app = App(sim=sim, scenario=scenario)
    app.mainloop()
