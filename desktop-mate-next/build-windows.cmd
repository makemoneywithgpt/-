@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
if exist "dist\KurimanjuMateNext" (
    echo Move the existing dist\KurimanjuMateNext folder aside before rebuilding.
    goto fail
)
call ".\setup-windows.cmd" build
if errorlevel 1 goto fail
"%mate_python%" -m unittest discover -s tests -v
if errorlevel 1 goto fail
"%mate_python%" -m tools.verify_bundle
if errorlevel 1 goto fail
"%mate_python%" -m PyInstaller --noconfirm --onedir --windowed --name KurimanjuMateNext run.py
if errorlevel 1 goto fail
"%mate_python%" -m tools.verify_bundle --bundle "dist\KurimanjuMateNext"
if errorlevel 1 goto fail
echo Build verified: dist\KurimanjuMateNext\KurimanjuMateNext.exe
popd
pause
exit /b 0

:fail
echo Build failed. See the error above.
popd
pause
exit /b 1
