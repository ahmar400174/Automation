@echo off
REM Starts the web app: double-click start.bat, then open http://localhost:8000
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (echo Run setup.bat first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
start "" http://localhost:8000
python app.py
pause
