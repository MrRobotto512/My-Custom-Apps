# Build RestoreDoctor.exe (one-folder) on Windows and bundle the engine next to it.
#
#   1. Build the engine once in the "MSYS2 MINGW64" shell:   ./tools/build_engine.sh
#      (or download the engine folder from the GitHub Actions build)
#   2. In PowerShell, from the project folder:              .\tools\build_app.ps1
#
# Result: dist\RestoreDoctor\RestoreDoctor.exe (+ engine\) and dist\RestoreDoctor-win64.zip
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip pyinstaller pytest
& .\.venv\Scripts\python.exe -m pytest -q tests\test_rules.py
if ($LASTEXITCODE -ne 0) { throw "Rule tests failed - not building." }

& .\.venv\Scripts\pyinstaller.exe --noconfirm RestoreDoctor.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

if (Test-Path "engine\idevicerestore.exe") {
    Copy-Item -Recurse -Force "engine" "dist\RestoreDoctor\engine"
    & "dist\RestoreDoctor\engine\idevicerestore.exe" -v
} else {
    Write-Warning "engine\idevicerestore.exe not found. Build it with tools/build_engine.sh (MSYS2 MINGW64), then rerun. The app will start, but its ready check will flag the missing engine."
}

Compress-Archive -Force -Path "dist\RestoreDoctor" -DestinationPath "dist\RestoreDoctor-win64.zip"
Write-Host "Done: dist\RestoreDoctor\RestoreDoctor.exe and dist\RestoreDoctor-win64.zip"
