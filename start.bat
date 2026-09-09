@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Zvezdochka VK-First Support Bot v5.7
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if not errorlevel 1 (py -3 -m venv .venv) else (python -m venv .venv)
)
set "PY=.venv\Scripts\python.exe"
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
"%PY%" scripts\preflight.py
if errorlevel 1 exit /b 1
if not exist ".env" (
  copy .env.example .env >nul
  echo Edit .env and set VK_GROUP_TOKEN and VK_GROUP_ID, then run again.
  pause
  exit /b 2
)
"%PY%" -m app.main
pause
