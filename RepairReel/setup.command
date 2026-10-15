#!/bin/bash
# Double-click in Finder to run the one-time setup in a Terminal window.
cd "$(dirname "$0")"
./setup.sh
echo
echo "Press Return to close this window."
read -r _
