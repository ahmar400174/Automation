@echo off
title WH Scanner
cd /d "%~dp0"
REM ===== ONE-CLICK START: double-click this file. First time it installs everything, then the dashboard opens. =====
REM A new start always stops the previous scanner, its Tor, and leftover browsers from this folder.
echo Stopping any previous WH Scanner from this folder...
powershell -NoProfile -Command "$root = [regex]::Escape((Resolve-Path '.').Path); Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match $root) -and ($_.Name -match 'python|tor|chrome|chromium|msedge') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul
set WH_OPEN_BROWSER=1
where python >nul 2>nul || (echo Python 3.10 or newer is not installed. Opening the download page... & start https://www.python.org/downloads/ & echo Install it, tick Add python.exe to PATH, then double-click RUN.bat again. & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
  echo First start: installing everything, 3 to 6 minutes, only once...
  python -m venv .venv || (pause & exit /b 1)
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt || (pause & exit /b 1)
  .venv\Scripts\python.exe -m playwright install chromium || (pause & exit /b 1)
)
echo Starting WH Scanner. Keep this window open until the work is finished.
.venv\Scripts\python.exe app.py
pause