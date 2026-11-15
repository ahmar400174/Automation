@echo off
REM One-time setup for Windows: double-click setup.bat
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.10+ is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
python -m venv .venv || (pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || (pause & exit /b 1)
python -m playwright install chromium || (pause & exit /b 1)
echo.
echo Setup finished. Start the app with start.bat
pause
