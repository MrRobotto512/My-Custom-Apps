#!/bin/bash
# RepairReel - one-time setup for macOS (Apple Silicon or Intel).
# Run it from Terminal:   cd "$(dirname "$0")" && ./setup.sh      or double-click setup.command
set -e
cd "$(dirname "$0")"
echo
echo "  RepairReel - one-time setup (macOS)"
echo "  -----------------------------------"

# --- Python
# pick a Python 3.10+ that also has Tk (needed for the window); 3.12/3.13 first, since the GPU wheels lag on brand-new releases
PY=""; PYNOTK=""
for c in python3.12 python3.13 python3.11 python3.10 python3.14 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null || continue
  if "$c" -c 'import tkinter' 2>/dev/null; then PY="$c"; break; fi
  [ -z "$PYNOTK" ] && PYNOTK="$c"
done
if [ -z "$PY" ]; then
  if [ -n "$PYNOTK" ]; then
    echo "  Found $PYNOTK but it has no Tk (needed for the window). Either install Python from https://www.python.org/downloads/macos/"
    echo "  or run:  brew install python-tk@$("$PYNOTK" -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  else
    echo "  Python 3.10+ not found. Install it from https://www.python.org/downloads/macos/ (includes Tk),"
    echo "  or with Homebrew:  brew install python@3.12 python-tk@3.12"
  fi
  exit 1
fi
echo "  Using $($PY --version) at $(command -v "$PY")"

# --- venv + packages
if [ ! -d .venv ]; then
  echo "  Creating a private Python environment in .venv ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
echo "  Installing packages (anthropic, numpy, pillow, whisper backend) - a few minutes the first time ..."
if ! pip install -r requirements.txt; then
  echo
  echo "  Package install failed. If the error mentions mlx-whisper or faster-whisper and you are on a very new Python"
  echo "  (3.14+), install Python 3.12 from https://www.python.org/downloads/macos/, delete the .venv folder, and rerun."
  exit 1
fi

# --- ffmpeg: Homebrew build has VideoToolbox + drawtext; otherwise a static copy into bin/
if [ ! -x bin/ffmpeg ]; then
  if command -v brew >/dev/null 2>&1; then
    if ! command -v ffmpeg >/dev/null 2>&1; then
      echo "  Installing ffmpeg with Homebrew ..."
      brew install ffmpeg
    else
      echo "  ffmpeg already installed: $(command -v ffmpeg)"
    fi
  else
    echo "  Homebrew not found; fetching a static ffmpeg via pip (static-ffmpeg) ..."
    python -c "from static_ffmpeg import run; ff, fp = run.get_or_fetch_platform_executables_else_raise(); print('  ffmpeg:', ff)" || \
      echo "  Could not fetch ffmpeg. Install Homebrew (https://brew.sh) and run: brew install ffmpeg"
  fi
fi

echo
echo "  Checking the GPU paths (MLX transcription on Apple Silicon, VideoToolbox encoding) ..."
python -m repairreel gpu-check || true
echo
echo "  Done. Start the app with ./run.sh (or double-click RepairReel.command)."
echo "  Set your API key first if you like:  ./run.sh set-key sk-ant-..."
