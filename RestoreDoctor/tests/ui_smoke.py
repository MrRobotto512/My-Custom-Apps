"""Headless UI walk-through in simulation mode (run under Xvfb).

    xvfb-run -s "-screen 0 1400x900x24" python tests/ui_smoke.py <scenario> [<scenario2>] [--shots DIR]

Starts the app, runs the scenario(s) as consecutive attempts on one phone,
and optionally saves screenshots of the Restore and Verdict tabs.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RD_DATA_DIR", tempfile.mkdtemp(prefix="rd-ui-"))

from restore_doctor.ui.app import App  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    shots = None
    if "--shots" in sys.argv:
        shots = Path(sys.argv[sys.argv.index("--shots") + 1])
        shots.mkdir(parents=True, exist_ok=True)
        args = [a for a in args if a != str(shots)]
    scenarios = args or ["baseband_1015"]
    app = App(sim=True, scenario=scenarios[0])
    app.geometry("1360x860+0+0")
    state = {"i": 0, "result": None}

    def shot(name):
        if shots:
            app.update_idletasks()
            app.update()
            subprocess.run(["import", "-window", "root", str(shots / f"{name}.png")], check=False)

    def kick():
        if not app.builds:
            app.after(300, kick)
            return
        sid = scenarios[state["i"]]
        app.sim_id.set(sid)
        app.session.sim_id = sid
        app.start()
        app.after(2500, lambda: shot(f"{state['i'] + 1}-{sid}-restore"))
        app.after(500, wait)

    def wait():
        s = app.session
        if s.running or not s.attempts or s.attempts[-1].ended_mono is None:
            app.after(300, wait)
            return
        app.after(600, after_attempt)

    def after_attempt():
        sid = scenarios[state["i"]]
        shot(f"{state['i'] + 1}-{sid}-verdict")
        v = app.session.verdict
        print(f"[{sid}] {v.category} / {v.confidence}: {v.headline}")
        state["i"] += 1
        if state["i"] < len(scenarios):
            app.after(300, kick)
        else:
            app.nb.select(app.tab_history)
            app.after(400, lambda: (shot("history"), finish()))

    def finish():
        app.code_var.set("4013")
        app.lookup_code()
        app.nb.select(app.tab_lookup)
        app.after(400, lambda: (shot("lookup"), app.destroy()))

    app.after(800, kick)
    app.after(240000, app.destroy)  # safety net
    app.mainloop()


if __name__ == "__main__":
    main()
