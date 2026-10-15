"""User settings stored in %APPDATA%\\RepairReel\\config.json (or ~/.config/RepairReel on other OSes)."""
from __future__ import annotations

import json
import os
import sys

DEFAULTS = {
    "api_key": "",
    "model": "auto",
    "whisper_model": "small.en",
    "jargon": "PS5, PS4, PS3, Xbox, Switch, HDMI, retimer, reflow, flux, solder, hot air, UART, APU, IC, PCB, "
              "liquid metal, thermal paste, BGA, multimeter, short circuit, NAND, eMMC, digitizer, Face ID",
    "planner": "claude",          # claude | signals
    "style": "story",             # story | montage
    "aspect": "16:9",
    "target_length": 240,
    "music": "",
    "music_volume_story": 0.12,
    "music_volume_montage": 0.9,
    "original_volume_montage": 0.25,
    "keep_original_audio": True,
    "captions": True,
    "title_card": True,
    "encoder": "auto",
    "crf": 20,
    "height": 1080,
    "last_folder": "",
    "notes": "",
}


def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "RepairReel")


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if not cfg.get("api_key"):
        cfg["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg


def save(cfg: dict) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
