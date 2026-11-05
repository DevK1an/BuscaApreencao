@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Execute instalar.cmd primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 hub.py %*
pause
