@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0tools\uv.exe" run --no-project --python 3.12 "%~dp0scripts\install_release.py"
if errorlevel 1 pause
