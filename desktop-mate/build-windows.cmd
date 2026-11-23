@echo off
setlocal
pushd "%~dp0" || exit /b 1
if exist "dist\KurimanjuMate" (
    echo Move the existing dist\KurimanjuMate folder aside before rebuilding.
    goto fail
)
call ".\setup-windows.cmd" build
if errorlevel 1 goto fail
".venv-windows\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto fail
".venv-windows\Scripts\python.exe" tools\verify_bundle.py
if errorlevel 1 goto fail
".venv-windows\Scripts\python.exe" -m PyInstaller --noconfirm --onedir --windowed --name KurimanjuMate run.py
if errorlevel 1 goto fail
".venv-windows\Scripts\python.exe" tools\verify_bundle.py --bundle "dist\KurimanjuMate"
if errorlevel 1 goto fail
echo Build verified: dist\KurimanjuMate\KurimanjuMate.exe
popd
pause
exit /b 0

:fail
echo Build failed. See the error above.
popd
pause
exit /b 1
