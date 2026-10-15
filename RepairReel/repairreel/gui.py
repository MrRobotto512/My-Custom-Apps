"""RepairReel desktop window (tkinter, ships with Python)."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import analyze, config, media, planner, render

APP = "RepairReel"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP} - turn a folder of repair clips into one video")
        self.geometry("1100x780")
        self.minsize(960, 680)
        self.cfg = config.load()
        self.data: dict | None = None
        self.plan: dict | None = None
        self.busy = False
        self.q: queue.Queue = queue.Queue()
        self._build()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._close)
        if self.cfg.get("last_folder") and os.path.isdir(self.cfg["last_folder"]):
            self.folder_var.set(self.cfg["last_folder"])
            self._load_existing()
        if not self.cfg.get("api_key"):
            self.log("No API key yet: open Settings and paste your Anthropic key, or use the 'signals only' planner.")

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Clips folder").pack(side="left")
        self.folder_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse...", command=self._pick_folder).pack(side="left")
        self.clips_lbl = ttk.Label(top, text="", foreground="gray")
        self.clips_lbl.pack(side="left", padx=8)

        opts = ttk.LabelFrame(self, text="What to make")
        opts.pack(fill="x", **pad)
        r1 = ttk.Frame(opts)
        r1.pack(fill="x", padx=6, pady=2)
        self.style_var = tk.StringVar(value=self.cfg["style"])
        ttk.Radiobutton(r1, text="Story  (narration-led, tells the repair)", variable=self.style_var, value="story", command=self._style_changed).pack(side="left")
        ttk.Radiobutton(r1, text="Montage  (fast cuts on the music)", variable=self.style_var, value="montage", command=self._style_changed).pack(side="left", padx=12)
        ttk.Label(r1, text="Target length").pack(side="left", padx=(20, 4))
        self.len_var = tk.IntVar(value=int(self.cfg["target_length"]))
        ttk.Spinbox(r1, from_=15, to=1800, increment=15, textvariable=self.len_var, width=6).pack(side="left")
        ttk.Label(r1, text="s").pack(side="left")
        ttk.Label(r1, text="Output").pack(side="left", padx=(20, 4))
        self.aspect_var = tk.StringVar(value=self.cfg["aspect"])
        ttk.Radiobutton(r1, text="16:9 YouTube", variable=self.aspect_var, value="16:9").pack(side="left")
        ttk.Radiobutton(r1, text="9:16 Short", variable=self.aspect_var, value="9:16").pack(side="left", padx=6)

        r2 = ttk.Frame(opts)
        r2.pack(fill="x", padx=6, pady=2)
        ttk.Label(r2, text="Music").pack(side="left")
        self.music_var = tk.StringVar(value=self.cfg.get("music", ""))
        ttk.Entry(r2, textvariable=self.music_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r2, text="...", width=3, command=self._pick_music).pack(side="left")
        ttk.Button(r2, text="none", width=5, command=lambda: self.music_var.set("")).pack(side="left", padx=(2, 0))
        self.music_hint = ttk.Label(r2, text="", foreground="gray")
        self.music_hint.pack(side="left", padx=8)

        r3 = ttk.Frame(opts)
        r3.pack(fill="x", padx=6, pady=2)
        ttk.Label(r3, text="Notes for the editor").pack(side="left")
        self.notes_var = tk.StringVar(value=self.cfg.get("notes", ""))
        e = ttk.Entry(r3, textvariable=self.notes_var)
        e.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(r3, text="Planner").pack(side="left", padx=(12, 4))
        self.planner_var = tk.StringVar(value=self.cfg["planner"])
        ttk.Combobox(r3, textvariable=self.planner_var, values=["claude", "signals"], state="readonly", width=8).pack(side="left")

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.b_analyze = ttk.Button(btns, text="1. Analyze clips", command=self._analyze)
        self.b_analyze.pack(side="left")
        self.b_plan = ttk.Button(btns, text="2. Plan the edit", command=self._plan)
        self.b_plan.pack(side="left", padx=6)
        self.b_render = ttk.Button(btns, text="3. Render video", command=self._render)
        self.b_render.pack(side="left")
        self.b_auto = ttk.Button(btns, text="Do all three", command=self._auto)
        self.b_auto.pack(side="left", padx=(18, 0))
        ttk.Button(btns, text="Settings...", command=self._settings).pack(side="right")
        self.b_open = ttk.Button(btns, text="Open output folder", command=self._open_folder)
        self.b_open.pack(side="right", padx=6)

        # plan table
        planf = ttk.LabelFrame(self, text="Edit plan  (double-click a row to adjust it)")
        planf.pack(fill="both", expand=True, **pad)
        tr = ttk.Frame(planf)
        tr.pack(fill="x", padx=6, pady=2)
        ttk.Label(tr, text="Title").pack(side="left")
        self.title_var = tk.StringVar()
        ttk.Entry(tr, textvariable=self.title_var).pack(side="left", fill="x", expand=True, padx=6)
        self.total_lbl = ttk.Label(tr, text="")
        self.total_lbl.pack(side="right")
        cols = ("n", "clip", "start", "end", "dur", "caption", "why")
        self.tree = ttk.Treeview(planf, columns=cols, show="headings", selectmode="browse", height=9)
        for c, w, txt in (("n", 36, "#"), ("clip", 200, "Clip"), ("start", 70, "Start"), ("end", 70, "End"), ("dur", 60, "Len"),
                          ("caption", 200, "Caption"), ("why", 320, "Why")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w" if c in ("clip", "caption", "why") else "e", stretch=c in ("clip", "caption", "why"))
        self.tree.pack(fill="both", expand=True, padx=6, pady=2)
        self.tree.bind("<Double-1>", lambda e: self._edit_row())
        pb = ttk.Frame(planf)
        pb.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(pb, text="Adjust", command=self._edit_row).pack(side="left")
        ttk.Button(pb, text="Remove", command=self._remove_row).pack(side="left", padx=4)
        ttk.Button(pb, text="Move up", command=lambda: self._move(-1)).pack(side="left")
        ttk.Button(pb, text="Move down", command=lambda: self._move(1)).pack(side="left", padx=4)
        ttk.Button(pb, text="Preview segment", command=self._preview_segment).pack(side="left", padx=(16, 0))
        self.summary_lbl = ttk.Label(pb, text="", foreground="gray")
        self.summary_lbl.pack(side="left", padx=12)

        # progress + log
        bottom = ttk.Frame(self)
        bottom.pack(fill="both", expand=True, **pad)
        self.prog = ttk.Progressbar(bottom, maximum=1000)
        self.prog.pack(fill="x")
        self.status = ttk.Label(bottom, text="Ready.")
        self.status.pack(anchor="w")
        self.logbox = tk.Text(bottom, height=8, wrap="word", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")
        self._style_changed()

    # ------------------------------------------------------------------ helpers
    def log(self, msg: str) -> None:
        self.q.put(("log", msg))

    def _pump(self) -> None:
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", val + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "progress":
                    self.prog["value"] = int(val * 1000)
                elif kind == "status":
                    self.status.configure(text=val)
                elif kind == "done":
                    self._set_busy(False)
                    val()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _set_busy(self, b: bool) -> None:
        self.busy = b
        state = "disabled" if b else "normal"
        for w in (self.b_analyze, self.b_plan, self.b_render, self.b_auto):
            w.configure(state=state)
        self.configure(cursor="watch" if b else "")

    def _run_bg(self, fn, on_done=None) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.prog["value"] = 0

        def work():
            try:
                result = fn()
                self.q.put(("done", lambda: on_done(result) if on_done else None))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.log("ERROR: " + msg)
                self.q.put(("status", "Failed - see log"))
                self.q.put(("done", lambda: messagebox.showerror(APP, msg[:1500])))

        threading.Thread(target=work, daemon=True).start()

    def _folder(self) -> str | None:
        f = self.folder_var.get().strip()
        if not f or not os.path.isdir(f):
            messagebox.showinfo(APP, "Pick the folder that holds the clips from one repair.")
            return None
        return f

    def _save_cfg(self) -> None:
        self.cfg.update({
            "style": self.style_var.get(), "target_length": int(self.len_var.get()), "aspect": self.aspect_var.get(),
            "music": self.music_var.get().strip(), "notes": self.notes_var.get(), "planner": self.planner_var.get(),
            "last_folder": self.folder_var.get().strip(),
        })
        config.save(self.cfg)

    def _style_changed(self) -> None:
        montage = self.style_var.get() == "montage"
        self.music_hint.configure(text="cuts land on the beat" if montage else "optional quiet music bed under the narration")

    # ------------------------------------------------------------------ actions
    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Folder with the clips from one repair", initialdir=self.folder_var.get() or None)
        if d:
            self.folder_var.set(d)
            self._load_existing()

    def _pick_music(self) -> None:
        f = filedialog.askopenfilename(title="Music track", filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus"), ("All", "*.*")])
        if f:
            self.music_var.set(f)

    def _load_existing(self) -> None:
        folder = self.folder_var.get().strip()
        try:
            clips = media.list_clips(folder)
        except Exception:
            clips = []
        self.clips_lbl.configure(text=f"{len(clips)} video files")
        self.data = analyze.load_cache(folder) or None
        if self.data and self.data.get("clips"):
            self.log(f"Found earlier analysis of {len(self.data['clips'])} clips in {folder}")
        else:
            self.data = None
        self.plan = planner.load_plan(folder)
        if self.plan:
            self.log("Found an earlier plan; you can render it or plan again.")
        self._show_plan()

    def _analyze(self, then=None) -> None:
        folder = self._folder()
        if not folder:
            return
        self._save_cfg()
        self.q.put(("status", "Analyzing clips (motion, audio, transcript, frames)..."))

        def fn():
            return analyze.analyze_folder(folder, self.log, whisper_model=self.cfg["whisper_model"], jargon=self.cfg["jargon"],
                                          progress=lambda f: self.q.put(("progress", f)))

        def done(data):
            self.data = data
            self.q.put(("status", f"Analyzed {len(data['clips'])} clips."))
            if then:
                then()

        self._run_bg(fn, done)

    def _plan(self, then=None) -> None:
        folder = self._folder()
        if not folder:
            return
        if not self.data:
            messagebox.showinfo(APP, "Analyze the clips first (step 1).")
            return
        self._save_cfg()
        style, length, aspect = self.style_var.get(), int(self.len_var.get()), self.aspect_var.get()
        notes, use_claude = self.notes_var.get(), self.planner_var.get() == "claude"
        if use_claude and not self.cfg.get("api_key"):
            messagebox.showinfo(APP, "Paste your Anthropic API key in Settings first, or switch the planner to 'signals'.")
            return
        self.q.put(("status", "Planning the edit..."))

        def fn():
            if use_claude:
                return planner.plan_with_claude(self.data, style, length, aspect, notes, self.cfg["api_key"], self.cfg["model"], self.log)
            return planner.plan_with_signals(self.data, style, length, aspect, self.log)

        def done(plan):
            self.plan = plan
            planner.save_plan(plan)
            self._show_plan()
            self.q.put(("status", f"Plan ready: {len(plan['segments'])} segments. Adjust if you like, then render."))
            if then:
                then()

        self._run_bg(fn, done)

    def _render(self) -> None:
        folder = self._folder()
        if not folder or not self.data:
            return
        if not self.plan or not self.plan.get("segments"):
            messagebox.showinfo(APP, "Make a plan first (step 2).")
            return
        self._save_cfg()
        self._collect_plan_edits()
        planner.save_plan(self.plan)
        opts = render.RenderOptions()
        opts.aspect = self.aspect_var.get()
        opts.music = self.music_var.get().strip()
        montage = self.plan.get("style") == "montage"
        opts.music_volume = self.cfg["music_volume_montage"] if montage else self.cfg["music_volume_story"]
        opts.original_volume = self.cfg["original_volume_montage"] if montage else 1.0
        opts.keep_original_audio = bool(self.cfg["keep_original_audio"])
        opts.captions, opts.title_card = bool(self.cfg["captions"]), bool(self.cfg["title_card"])
        opts.encoder, opts.crf, opts.height = self.cfg["encoder"], int(self.cfg["crf"]), int(self.cfg["height"])
        default = _safe(self.plan.get("title") or os.path.basename(folder)) + ("_short" if opts.aspect == "9:16" else "_highlight") + ".mp4"
        out = filedialog.asksaveasfilename(title="Save video as", initialdir=folder, initialfile=default, defaultextension=".mp4",
                                           filetypes=[("MP4 video", "*.mp4")])
        if not out:
            return
        self.q.put(("status", "Rendering..."))
        plan, data = self.plan, self.data

        def fn():
            return render.render(plan, data, out, opts, self.log, progress=lambda f: self.q.put(("progress", f)))

        def done(path):
            self.q.put(("status", f"Done: {path}"))
            self.last_output = path
            if messagebox.askyesno(APP, f"Rendered:\n{path}\n\nOpen it now?"):
                _open(path)

        self._run_bg(fn, done)

    def _auto(self) -> None:
        self._analyze(then=lambda: self._plan(then=self._render))

    def _open_folder(self) -> None:
        f = self.folder_var.get().strip()
        if f and os.path.isdir(f):
            _open(f)

    # ------------------------------------------------------------------ plan table
    def _show_plan(self) -> None:
        for i in self.tree.get_children():
            self.tree.delete(i)
        if not self.plan:
            self.title_var.set("")
            self.total_lbl.configure(text="")
            self.summary_lbl.configure(text="")
            return
        self.title_var.set(self.plan.get("title", ""))
        total = 0.0
        for i, s in enumerate(self.plan["segments"]):
            d = s["end"] - s["start"]
            total += d
            self.tree.insert("", "end", iid=str(i), values=(i + 1, s["file"], f"{s['start']:.1f}", f"{s['end']:.1f}", f"{d:.1f}", s.get("caption", ""), s.get("why", "")))
        self.total_lbl.configure(text=f"{len(self.plan['segments'])} segments, {media.fmt_time(total)} total   ({self.plan.get('style')}, by {self.plan.get('planner')})")
        self.summary_lbl.configure(text=self.plan.get("summary", "")[:140])

    def _collect_plan_edits(self) -> None:
        if self.plan:
            self.plan["title"] = self.title_var.get().strip()
            self.plan["aspect"] = self.aspect_var.get()

    def _selected(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _edit_row(self) -> None:
        i = self._selected()
        if i is None or not self.plan:
            return
        s = self.plan["segments"][i]
        clip = next((c for c in self.data["clips"] if c["index"] == s["clip"]), None) if self.data else None
        dur = clip["duration"] if clip else 1e9
        dlg = tk.Toplevel(self)
        dlg.title(f"Adjust segment {i + 1} - {s['file']}")
        dlg.transient(self)
        dlg.grab_set()
        vs = {k: tk.StringVar(value=str(s[k])) for k in ("start", "end", "caption", "crop_x")}
        rows = (("Start (s)", "start"), ("End (s)", "end"), ("Caption", "caption"), ("Focus x for vertical crop (0-1)", "crop_x"))
        for r, (lbl, k) in enumerate(rows):
            ttk.Label(dlg, text=lbl).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(dlg, textvariable=vs[k], width=40).grid(row=r, column=1, padx=8, pady=4)
        ttk.Label(dlg, text=f"clip is {dur:.1f}s long", foreground="gray").grid(row=len(rows), column=1, sticky="w", padx=8)

        def ok():
            try:
                a, b = float(vs["start"].get()), float(vs["end"].get())
                cx = float(vs["crop_x"].get())
            except ValueError:
                messagebox.showerror(APP, "Start, end and focus must be numbers.", parent=dlg)
                return
            a, b = max(0.0, min(a, dur)), max(0.0, min(b, dur))
            if b - a < 0.5:
                messagebox.showerror(APP, "Segment must be at least 0.5 s.", parent=dlg)
                return
            s.update({"start": round(a, 2), "end": round(b, 2), "caption": vs["caption"].get().strip()[:60], "crop_x": min(1.0, max(0.0, cx))})
            dlg.destroy()
            self._show_plan()
            self.tree.selection_set(str(i))

        ttk.Button(dlg, text="OK", command=ok).grid(row=len(rows) + 1, column=1, sticky="e", padx=8, pady=8)
        dlg.bind("<Return>", lambda e: ok())

    def _remove_row(self) -> None:
        i = self._selected()
        if i is None or not self.plan:
            return
        del self.plan["segments"][i]
        self._show_plan()

    def _move(self, d: int) -> None:
        i = self._selected()
        if i is None or not self.plan:
            return
        j = i + d
        segs = self.plan["segments"]
        if 0 <= j < len(segs):
            segs[i], segs[j] = segs[j], segs[i]
            self._show_plan()
            self.tree.selection_set(str(j))

    def _preview_segment(self) -> None:
        """Play just this segment with ffplay if available, else open the clip."""
        i = self._selected()
        if i is None or not self.plan or not self.data:
            return
        s = self.plan["segments"][i]
        clip = next((c for c in self.data["clips"] if c["index"] == s["clip"]), None)
        if not clip:
            return
        ff, _ = media.find_tools()
        ffplay = os.path.join(os.path.dirname(ff), "ffplay" + (".exe" if sys.platform == "win32" else ""))
        if os.path.isfile(ffplay):
            subprocess.Popen([ffplay, "-autoexit", "-ss", str(s["start"]), "-t", str(s["end"] - s["start"]), "-window_title",
                              f"segment {i + 1}", clip["path"]])
        else:
            _open(clip["path"])

    # ------------------------------------------------------------------ settings
    def _settings(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Settings")
        dlg.transient(self)
        dlg.grab_set()
        fields = [
            ("Anthropic API key", "api_key", "sk-ant-...  (console.anthropic.com)"),
            ("Model", "model", "auto = newest Sonnet; or a model id"),
            ("Whisper model", "whisper_model", "tiny.en / base.en / small.en / medium.en / large-v3-turbo / none"),
            ("Repair jargon for the transcriber", "jargon", "comma-separated words it should spell right"),
            ("Video encoder", "encoder", "auto = hardware encoder when available (NVENC on Windows, VideoToolbox on Mac), else CPU libx264"),
            ("Quality (CRF, lower = better)", "crf", "18-23"),
            ("Output height", "height", "1080 or 2160"),
            ("Music level in montage (0-1)", "music_volume_montage", ""),
            ("Original audio under music in montage (0-1)", "original_volume_montage", ""),
            ("Music bed level in story (0-1)", "music_volume_story", ""),
        ]
        vs = {}
        for r, (lbl, key, hint) in enumerate(fields):
            ttk.Label(dlg, text=lbl).grid(row=r, column=0, sticky="w", padx=8, pady=3)
            vs[key] = tk.StringVar(value=str(self.cfg.get(key, "")))
            ttk.Entry(dlg, textvariable=vs[key], width=48, show="*" if key == "api_key" else "").grid(row=r, column=1, padx=8, pady=3)
            ttk.Label(dlg, text=hint, foreground="gray").grid(row=r, column=2, sticky="w", padx=4)
        bools = [("Burn captions", "captions"), ("Title card at the start", "title_card"), ("Keep original audio under music", "keep_original_audio")]
        bv = {}
        for r, (lbl, key) in enumerate(bools, start=len(fields)):
            bv[key] = tk.BooleanVar(value=bool(self.cfg.get(key, True)))
            ttk.Checkbutton(dlg, text=lbl, variable=bv[key]).grid(row=r, column=1, sticky="w", padx=8, pady=2)

        def ok():
            for key, v in vs.items():
                val = v.get().strip()
                if key in ("crf", "height"):
                    try:
                        val = int(val)
                    except ValueError:
                        continue
                elif key.startswith(("music_volume", "original_volume")):
                    try:
                        val = float(val)
                    except ValueError:
                        continue
                self.cfg[key] = val
            for key, v in bv.items():
                self.cfg[key] = bool(v.get())
            config.save(self.cfg)
            dlg.destroy()

        ttk.Button(dlg, text="Save", command=ok).grid(row=len(fields) + len(bools), column=1, sticky="e", padx=8, pady=8)

    def _close(self) -> None:
        if self.busy and not messagebox.askyesno(APP, "Still working. Quit anyway?"):
            return
        self._save_cfg()
        self.destroy()


def _safe(s: str) -> str:
    import re

    s = re.sub(r"[^\w\- ]+", "", s).strip().replace(" ", "_")
    return s[:60] or "highlight"


def _open(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
