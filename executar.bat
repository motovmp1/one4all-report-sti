@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: The .venv folder was not found.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "main.py"
