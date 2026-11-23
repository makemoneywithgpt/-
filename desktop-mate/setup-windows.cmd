@echo off
setlocal
pushd "%~dp0" || exit /b 1

if exist ".venv\Scripts\python.exe" goto check
py -3.12 -c "import sys, sysconfig; sys.exit(not (sys.version_info[:2] == (3, 12) and sysconfig.get_platform() == 'win-amd64'))" >nul 2>&1
if errorlevel 1 goto fallback
py -3.12 -m venv .venv
if errorlevel 1 goto fail
goto check

:fallback
python -c "import sys, sysconfig; sys.exit(not (sys.version_info[:2] == (3, 12) and sysconfig.get_platform() == 'win-amd64'))" >nul 2>&1
if errorlevel 1 (
    echo Python 3.12 64-bit is required. Install it with the Python launcher or add it to PATH.
    goto fail
)
python -m venv .venv
if errorlevel 1 goto fail

:check
".venv\Scripts\python.exe" -c "import sys, sysconfig; sys.exit(not (sys.version_info[:2] == (3, 12) and sysconfig.get_platform() == 'win-amd64'))"
if errorlevel 1 (
    echo Use a Python 3.12 x64 virtual environment. Move an incompatible .venv aside and retry.
    goto fail
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
popd
exit /b 0

:fail
popd
exit /b 1
