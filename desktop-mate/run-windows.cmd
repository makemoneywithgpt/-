@echo off
setlocal
pushd "%~dp0" || exit /b 1
call setup-windows.cmd
if errorlevel 1 goto fail
".venv\Scripts\python.exe" run.py
if errorlevel 1 goto fail
popd
exit /b 0

:fail
echo Launch failed. See the error above.
popd
pause
exit /b 1
