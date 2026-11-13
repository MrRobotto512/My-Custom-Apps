"""The small status window a restore station shows on the PC it runs on.

The work is all in station.py; this is only so the person at the PC can see
that the station is up, what it is doing, and close it.
"""
from __future__ import annotations

import tkinter as tk

from .. import APP_NAME, __version__


def show(st) -> None:
    root = tk.Tk()
    root.title("%s %s - Restore station" % (APP_NAME, __version__))
    root.geometry("560x210")
    root.configure(bg="#1e1e20")
    fg, dim = "#f5f5f7", "#a1a1a8"

    def label(text="", size=10, color=fg, bold=False):
        w = tk.Label(root, text=text, bg="#1e1e20", fg=color, anchor="w", justify="left",
                     font=("Segoe UI", size, "bold" if bold else "normal"), wraplength=530)
        w.pack(fill="x", padx=16, pady=(8 if bold else 2, 0))
        return w

    label("Restore station: %s" % st.name, 13, bold=True)
    link = label(color=dim)
    phone = label()
    work = label()
    label("Start restores from the Hero Dashboard (Bench work > Restore Doctor). "
          "Leave this window open; closing it stops the station.", 9, dim)
    tk.Button(root, text="Stop the station", command=root.destroy).pack(anchor="e", padx=16, pady=10)

    def refresh():
        d = st.session.device
        link.configure(text=st.status_line)
        phone.configure(text="Phone: %s" % (("%s (%s)" % (d.name or d.product or "iPhone", d.mode))
                                            if d.connected else "none on USB"))
        j = st.job or {}
        txt = st.state
        if j.get("phase"):
            pct = j.get("pct")
            txt = "%s - %s%s %s" % (st.state, j["phase"],
                                    (" %d%%" % round(pct * 100)) if isinstance(pct, (int, float)) else "",
                                    j.get("msg") or j.get("headline") or "")
        work.configure(text="Now: " + txt)
        root.after(1000, refresh)

    refresh()
    root.mainloop()
