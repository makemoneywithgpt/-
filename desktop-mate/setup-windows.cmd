@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "mate_python=.venv-windows\Scripts\python.exe"
set "mate_check=import sys, sysconfig; sys.exit(not ((3, 12) <= sys.version_info[:2] < (3, 16) and sysconfig.get_platform() == 'win-amd64' and not sysconfig.get_config_var('Py_GIL_DISABLED')))"

if exist "%mate_python%" goto check
py -3.15 -c "%mate_check%" >nul 2>&1
if not errorlevel 1 (
    py -3.15 -m venv .venv-windows
    if errorlevel 1 goto fail
    goto check
)
python -c "%mate_check%" >nul 2>&1
if not errorlevel 1 (
    python -m venv .venv-windows
    if errorlevel 1 goto fail
    goto check
)
py -3 -c "%mate_check%" >nul 2>&1
if errorlevel 1 (
    echo Standard CPython 3.12 through 3.15 x64 is required.
    echo Make it available through the Python launcher or PATH.
    goto fail
)
py -3 -m venv .venv-windows
if errorlevel 1 goto fail

:check
"%mate_python%" -c "%mate_check%"
if errorlevel 1 (
    echo Move the incompatible .venv-windows folder aside and retry.
    goto fail
)
set "mate_requirements=requirements.txt"
if /i "%~1"=="build" set "mate_requirements=requirements-build.txt"
"%mate_python%" -m pip install --disable-pip-version-check --only-binary=:all: -r "%mate_requirements%"
if errorlevel 1 goto fail
"%mate_python%" -c "from PySide6.QtGui import QMovie; from PySide6.QtWidgets import QApplication"
if errorlevel 1 goto fail
popd
exit /b 0

:fail
popd
exit /b 1
