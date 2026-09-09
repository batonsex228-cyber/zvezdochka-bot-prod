@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once first so the virtual environment and packages are created.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\self_test.py
set "EXITCODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXITCODE%
