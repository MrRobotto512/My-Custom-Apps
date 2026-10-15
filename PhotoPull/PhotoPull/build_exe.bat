@echo off
REM Builds dist\PhotoPull.exe (single file, no console window).
cd /d "%~dp0"
call setup_env.bat pyinstaller || (echo. & echo Setup failed - see messages above. & pause & exit /b 1)
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed --name PhotoPull ^
  --collect-all pymobiledevice3 --collect-all pillow_heif ^
  main.py || (pause & exit /b 1)
echo.
echo Built: %cd%\dist\PhotoPull.exe
pause
