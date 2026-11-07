@echo off
setlocal
cd /d "%~dp0"
title Afterparty - TEST MODE
echo TEST MODE: sample data only, separate from real registrations.
where py >nul 2>nul
if errorlevel 1 (echo Python not found. Run START_HERE.bat first for instructions. & pause & exit /b 1)
if not exist ".venv\Scripts\python.exe" (py -3 -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install --quiet -r requirements.txt
set DEMO_MODE=1
echo.
echo Open http://127.0.0.1:5000/admin/login   (login: demo / demo-password-123)
echo Click "Add 3 sample registrations", then approve one and try the scanner.
start "" http://127.0.0.1:5000
python app.py
pause
