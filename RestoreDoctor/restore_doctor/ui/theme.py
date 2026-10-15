"""Colours, fonts and ttk styles. Dark, high-contrast, readable from the bench."""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

BG = "#0f1115"
PANEL = "#161a21"
CARD = "#1c212b"
CARD_HI = "#232937"
LINE = "#2b3140"
INK = "#e9ecf2"
QUIET = "#97a1b3"
FAINT = "#5d6678"
ACCENT = "#4f8ff7"
GOOD = "#2fbf71"
WARN = "#f2a93b"
BAD = "#f05252"
MONO_BG = "#11141a"

CATEGORY_COLOR = {"success": GOOD, "hardware": BAD, "setup": WARN, "firmware": WARN,
                  "inconclusive": ACCENT, "cancelled": QUIET}
MODE_COLOR = {"DFU": "#a78bfa", "Recovery": WARN, "Normal": GOOD, "Restore": ACCENT, "None": FAINT,
              "Gone": FAINT, "WTF": "#a78bfa", "Port DFU": "#a78bfa"}


def family() -> str:
    if sys.platform.startswith("win"):
        return "Segoe UI"
    return "DejaVu Sans"


def mono() -> str:
    return "Consolas" if sys.platform.startswith("win") else "DejaVu Sans Mono"


class Fonts:
    def __init__(self, root: tk.Misc):
        f = family()
        self.base = tkfont.Font(root, family=f, size=10)
        self.small = tkfont.Font(root, family=f, size=9)
        self.bold = tkfont.Font(root, family=f, size=10, weight="bold")
        self.h3 = tkfont.Font(root, family=f, size=11, weight="bold")
        self.h2 = tkfont.Font(root, family=f, size=13, weight="bold")
        self.h1 = tkfont.Font(root, family=f, size=17, weight="bold")
        self.big = tkfont.Font(root, family=f, size=22, weight="bold")
        self.mono = tkfont.Font(root, family=mono(), size=9)
        self.icon = tkfont.Font(root, family=f, size=13, weight="bold")


def apply(root: tk.Tk, fonts: Fonts) -> None:
    root.configure(bg=BG)
    root.option_add("*Font", fonts.base)
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=INK, fieldbackground=CARD, bordercolor=LINE,
                lightcolor=LINE, darkcolor=LINE, troughcolor=PANEL, focuscolor=ACCENT, font=fonts.base)
    s.configure("TFrame", background=BG)
    s.configure("Panel.TFrame", background=PANEL)
    s.configure("Card.TFrame", background=CARD)
    s.configure("TLabel", background=BG, foreground=INK)
    s.configure("Panel.TLabel", background=PANEL, foreground=INK)
    s.configure("PanelQuiet.TLabel", background=PANEL, foreground=QUIET, font=fonts.small)
    s.configure("PanelHead.TLabel", background=PANEL, foreground=QUIET, font=fonts.bold)
    s.configure("Card.TLabel", background=CARD, foreground=INK)
    s.configure("CardQuiet.TLabel", background=CARD, foreground=QUIET, font=fonts.small)
    s.configure("Quiet.TLabel", background=BG, foreground=QUIET)
    s.configure("H1.TLabel", background=BG, foreground=INK, font=fonts.h1)
    s.configure("H2.TLabel", background=BG, foreground=INK, font=fonts.h2)
    s.configure("TButton", background=CARD_HI, foreground=INK, borderwidth=1, padding=(12, 6),
                relief="flat")
    s.map("TButton", background=[("disabled", PANEL), ("pressed", LINE), ("active", LINE)],
          foreground=[("disabled", FAINT)])
    s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", font=fonts.h3, padding=(16, 10))
    s.map("Accent.TButton", background=[("disabled", LINE), ("pressed", "#3a74d6"), ("active", "#6aa2ff")],
          foreground=[("disabled", FAINT)])
    s.configure("Danger.TButton", background="#3a1d22", foreground="#ffb4b4")
    s.map("Danger.TButton", background=[("disabled", PANEL), ("active", "#55262d")],
          foreground=[("disabled", FAINT)])
    s.configure("TCheckbutton", background=PANEL, foreground=INK)
    s.map("TCheckbutton", background=[("active", PANEL)], indicatorcolor=[("selected", ACCENT)])
    s.configure("TRadiobutton", background=PANEL, foreground=INK)
    s.map("TRadiobutton", background=[("active", PANEL)], indicatorcolor=[("selected", ACCENT)])
    s.configure("TEntry", fieldbackground=CARD, foreground=INK, insertcolor=INK, padding=4)
    s.configure("TCombobox", fieldbackground=CARD, foreground=INK, arrowcolor=QUIET, padding=4)
    s.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", INK)])
    s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    s.configure("TNotebook.Tab", background=PANEL, foreground=QUIET, padding=(16, 8), borderwidth=0)
    s.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", INK)])
    s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=CARD, bordercolor=CARD,
                lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
    s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK, rowheight=26,
                bordercolor=LINE)
    s.configure("Treeview.Heading", background=PANEL, foreground=QUIET, relief="flat", font=fonts.bold)
    s.map("Treeview", background=[("selected", "#2b4a7d")])
    s.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, arrowcolor=QUIET, bordercolor=BG)
    s.configure("TSeparator", background=LINE)
