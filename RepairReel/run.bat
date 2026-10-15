@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo  Run setup.bat first.
    pause
    exit /b 1
)
if "%~1"=="" (
    start "" .venv\Scripts\pythonw.exe RepairReel.pyw
) else (
    .venv\Scripts\python.exe -m repairreel %*
)
