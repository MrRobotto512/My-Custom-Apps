@echo off
REM Makes the private Python environment in .venv using Python 3.12.
REM Why 3.12: two parts of the iPhone library (lzfse, pylzss) only ship ready-made
REM Windows builds up to Python 3.12. On 3.13/3.14 pip tries to compile them and fails.
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" && goto :deps
  echo Removing an old .venv made with a different Python version...
  rmdir /s /q .venv
)

call :findpy
if not defined PY312 (
  echo Python 3.12 not found. Installing it just for this user with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  call :findpy
)
if not defined PY312 (
  echo.
  echo Could not find or install Python 3.12.
  echo Get the Windows installer for 3.12 from https://www.python.org/downloads/windows/
  echo then run this again. Your other Python versions can stay installed.
  exit /b 1
)
echo Using Python 3.12: %PY312%
"%PY312%" -m venv .venv || exit /b 1

:deps
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install --only-binary=lzfse,pylzss -r requirements.txt %* || exit /b 1
exit /b 0

:findpy
set "PY312="
for /f "delims=" %%i in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do set "PY312=%%i"
if not defined PY312 if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY312 if exist "%ProgramFiles%\Python312\python.exe" set "PY312=%ProgramFiles%\Python312\python.exe"
exit /b 0
