#!/bin/bash
#  BenchTool Mac Diagnostics - double-click this to start.
#  Opens the live dashboard in this Mac's browser (Summary / Performance /
#  Processes / System Info / Startup / Users / Services / Disks / Tools /
#  Reports). The individual tools live in Tools/ and can be run from the
#  dashboard's Tools page or double-clicked there directly.
DIR="$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash "$DIR/Tools/Dashboard/Run-Dashboard.command" "$@"
