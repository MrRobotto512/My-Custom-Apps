#!/bin/bash
# Double-click in Finder to open RepairReel.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  osascript -e 'display dialog "Run setup.command first (one-time setup)." buttons {"OK"}' >/dev/null 2>&1
  exit 1
fi
.venv/bin/python RepairReel.pyw >/dev/null 2>&1 &
