@echo off
setlocal
cd /d "%~dp0"
echo.
echo  RepairReel - one-time setup
echo  ---------------------------
echo  Needs Python 3.10 or newer from https://www.python.org/downloads/windows/
echo  (tick "Add python.exe to PATH" in the installer).
echo.

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo  Python was not found. Install it from python.org, then run setup.bat again.
    pause
    exit /b 1
)

if not exist .venv (
    echo  Creating a private Python environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 ( echo  Could not create .venv & pause & exit /b 1 )
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
echo  Installing packages (anthropic, faster-whisper, CUDA libraries, numpy, pillow, ffmpeg) - several minutes the first time ...
pip install -r requirements.txt
if errorlevel 1 ( echo  Package install failed - see above. & pause & exit /b 1 )

if exist bin\ffmpeg.exe (
    echo  ffmpeg already in bin\ - keeping it.
    goto :ffdone
)
echo  Fetching ffmpeg 7.1.1 ^(Gyan essentials build: NVENC + drawtext, works with NVIDIA drivers 551+^) ...
set "FFURL=https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-essentials_build.zip"
curl.exe -L --fail --progress-bar -o ffmpeg_dl.zip "%FFURL%"
if errorlevel 1 (
    echo  Download failed. Get it by hand: %FFURL%
    echo  and copy ffmpeg.exe, ffprobe.exe, ffplay.exe from its bin\ folder into a bin\ folder here.
    del ffmpeg_dl.zip 2>nul
    goto :ffdone
)
if not exist bin mkdir bin
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force -LiteralPath 'ffmpeg_dl.zip' -DestinationPath 'ffmpeg_tmp'; Get-ChildItem -Recurse ffmpeg_tmp -Include ffmpeg.exe,ffprobe.exe,ffplay.exe | Move-Item -Force -Destination bin; Remove-Item -Recurse -Force ffmpeg_tmp"
if exist bin\ffmpeg.exe (
    del ffmpeg_dl.zip
    echo  OK - ffmpeg installed to bin\
) else (
    echo  Could not unzip automatically. Unzip ffmpeg_dl.zip by hand and copy ffmpeg.exe, ffprobe.exe, ffplay.exe from its bin\ into bin\ here.
)
:ffdone

echo.
echo  Checking the GPU (Whisper on CUDA + NVENC video encoding) ...
python -m repairreel gpu-check
echo.
echo  Done. Start the app with run.bat  (or set your API key first: run.bat set-key sk-ant-...)
pause
