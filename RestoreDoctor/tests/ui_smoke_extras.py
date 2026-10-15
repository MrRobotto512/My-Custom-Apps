"""Headless check of the DFU helper, log import, History re-diagnosis and a
no-phone run (simulation). Usage: xvfb-run python tests/ui_smoke_extras.py SHOTS_DIR"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RD_DATA_DIR", tempfile.mkdtemp(prefix="rd-ui-"))

from restore_doctor.ui import app as appmod  # noqa: E402

SC = Path(__file__).resolve().parent.parent / "restore_doctor" / "scenarios"


def main():
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    app = appmod.App(sim=True, scenario="faceid_savage")
    app.geometry("1360x860+0+0")
    appmod.messagebox.showinfo = lambda *a, **k: None  # no modal popups headless
    appmod.messagebox.askyesno = lambda *a, **k: True
    results = {}

    def shot(name):
        if shots:
            app.update_idletasks(); app.update()
            subprocess.run(["import", "-window", "root", str(shots / f"{name}.png")], check=False)

    def step1():
        app.show_dfu_helper()
        app.after(500, lambda: (shot("dfu-helper"), app._dfu_win.destroy(), app.after(200, step2)))

    def step2():
        appmod.filedialog.askopenfilename = lambda **k: str(SC / "restore_os_pc_side.log")
        app.import_log()
        results["import"] = app.vscroll.body.winfo_children() != []
        app.after(400, lambda: (shot("import"), step3()))

    def step3():
        if not app.builds:
            app.after(300, step3); return
        app.start()
        app.after(400, wait)

    def wait():
        s = app.session
        if s.running or not s.attempts or s.attempts[-1].ended_mono is None:
            app.after(300, wait); return
        results["run"] = app.session.verdict.headline
        app.refresh_history()
        first = app.hist.get_children()[0]
        app.hist.selection_set(first)
        app.history_show()
        app.after(500, lambda: (shot("history-verdict"), step4()))

    def step4():
        app.sim_id.set("no_device"); app._sim_changed()
        app.after(1500, step5)

    def step5():
        app.new_phone()
        app.after(1500, lambda: (app.start(), app.after(400, wait2)))

    def wait2():
        s = app.session
        if s.running or not s.attempts or s.attempts[-1].ended_mono is None:
            app.after(300, wait2); return
        results["no_device"] = app.session.verdict.headline
        app.after(400, lambda: (shot("no-device"), done()))

    def done():
        for k, v in results.items():
            print(k, "->", v)
        app.destroy()

    app.after(900, step1)
    app.after(200000, app.destroy)
    app.mainloop()


if __name__ == "__main__":
    main()
