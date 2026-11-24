@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
if not defined LOCALAPPDATA (
    echo LOCALAPPDATA is unavailable. Run this script from your Windows user session.
    goto fail
)
set "mate_venv=%LOCALAPPDATA%\KMate\v"
set "mate_python=%mate_venv%\Scripts\python.exe"
set "mate_check=import sys, sysconfig; sys.exit(not ((3, 12) <= sys.version_info[:2] < (3, 16) and sysconfig.get_platform() == 'win-amd64' and not sysconfig.get_config_var('Py_GIL_DISABLED')))"

if exist "%mate_python%" goto check
py -3.15 -c "%mate_check%" >nul 2>&1
if not errorlevel 1 (
    py -3.15 -m venv "%mate_venv%"
    if errorlevel 1 goto fail
    goto check
)
python -c "%mate_check%" >nul 2>&1
if not errorlevel 1 (
    python -m venv "%mate_venv%"
    if errorlevel 1 goto fail
    goto check
)
py -3 -c "%mate_check%" >nul 2>&1
if errorlevel 1 (
    echo Standard CPython 3.12 through 3.15 x64 is required.
    echo Make it available through the Python launcher or PATH.
    goto fail
)
py -3 -m venv "%mate_venv%"
if errorlevel 1 goto fail

:check
"%mate_python%" -c "%mate_check%"
if errorlevel 1 (
    echo Move the incompatible virtual environment aside and retry.
    echo "%mate_venv%"
    goto fail
)
set "mate_requirements=requirements.txt"
if /i "%~1"=="build" set "mate_requirements=requirements-build.txt"
"%mate_python%" -m pip install --disable-pip-version-check --only-binary=:all: -r "%mate_requirements%"
if errorlevel 1 goto fail
"%mate_python%" -c "from PySide6.QtGui import QMovie; from PySide6.QtWidgets import QApplication"
if errorlevel 1 goto fail
popd
endlocal & set "mate_python=%mate_python%" & exit /b 0

:fail
popd
exit /b 1
