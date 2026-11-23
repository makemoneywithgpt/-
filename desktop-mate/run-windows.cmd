@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
call ".\setup-windows.cmd"
if errorlevel 1 goto fail
"%mate_python%" run.py
if errorlevel 1 goto fail
popd
exit /b 0

:fail
echo Launch failed. See the error above.
popd
pause
exit /b 1
