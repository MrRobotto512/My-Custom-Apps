# RepairReel – working notes

Python 3.10+ desktop app (Tkinter GUI + CLI) that turns a folder of repair clips into one edited video (story or beat-synced montage). Claude plans the edit; analysis and rendering run locally with ffmpeg + Whisper.

## Setup / run (Windows)
- One-time: `setup.bat` → creates `.venv`, `pip install -r requirements.txt`, downloads ffmpeg 7.1.1 into `bin\`, runs `gpu-check`.
- GUI: `run.bat` (no args) → `.venv\Scripts\pythonw.exe RepairReel.pyw`
- CLI: `run.bat <cmd> ...` → `.venv\Scripts\python.exe -m repairreel <cmd>`
  - commands: `analyze`, `plan`, `render`, `auto`, `set-key`, `gpu-check`
- macOS: `setup.command`/`setup.sh`, `RepairReel.command`/`run.sh`.
- No build step and no test suite.

## Layout
- `repairreel/media.py` ffmpeg/ffprobe helpers · `analyze.py` per-clip analysis + Whisper + cache (`<clips>/.repairreel/`)
- `beats.py` music beat grid · `planner.py` Claude prompt/tool schema, signals planner, plan.json
- `render.py` ffmpeg filter graph/encoding · `gui.py` window · `cli.py` argparse CLI · `config.py` settings

## Environment state (2026-10-29)
- `.venv` exists (Python 3.14.7), `bin\` has ffmpeg/ffprobe/ffplay.
