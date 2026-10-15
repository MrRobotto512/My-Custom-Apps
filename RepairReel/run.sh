#!/bin/bash
# Start the RepairReel window, or run a CLI command:  ./run.sh plan "/path/to/clips" --style story
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Run ./setup.sh first."
  exit 1
fi
if [ $# -eq 0 ]; then
  exec .venv/bin/python RepairReel.pyw
else
  exec .venv/bin/python -m repairreel "$@"
fi
