"""Where things live: bundled resources, the engine folder, and per-user data."""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def resource_dir() -> Path:
    """Folder holding kb/ and scenarios/ (inside the PyInstaller bundle when frozen)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "restore_doctor"
    return Path(__file__).resolve().parent


def app_root() -> Path:
    """Folder the app runs from: next to RestoreDoctor.exe, or the project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def engine_dir() -> Path:
    """Folder with idevicerestore and friends. Override with RD_ENGINE_DIR."""
    env = os.environ.get("RD_ENGINE_DIR")
    if env:
        return Path(env)
    return app_root() / "engine"


def engine_exe(name: str) -> Path:
    exe = name + (".exe" if IS_WINDOWS else "")
    return engine_dir() / exe


def data_dir() -> Path:
    """Per-user data: cases, IPSW cache, settings. Override with RD_DATA_DIR."""
    env = os.environ.get("RD_DATA_DIR")
    if env:
        path = Path(env)
    elif IS_WINDOWS:
        path = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "RestoreDoctor"
    else:
        path = Path.home() / ".restore-doctor"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cases_dir() -> Path:
    path = data_dir() / "cases"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ipsw_dir() -> Path:
    """Default IPSW library. The tech can point it elsewhere in Settings."""
    path = data_dir() / "ipsw"
    path.mkdir(parents=True, exist_ok=True)
    return path


def simulation_enabled() -> bool:
    return os.environ.get("RD_SIM", "").strip() not in ("", "0", "false", "no")
