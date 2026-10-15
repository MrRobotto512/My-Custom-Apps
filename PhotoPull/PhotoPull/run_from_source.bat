@echo off
REM Runs PhotoPull straight from source (no .exe).
cd /d "%~dp0"
call setup_env.bat || (echo. & echo Setup failed - see messages above. & pause & exit /b 1)
start "" ".venv\Scripts\pythonw.exe" main.py
