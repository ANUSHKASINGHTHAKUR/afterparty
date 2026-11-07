@echo off
setlocal
cd /d "%~dp0"
title Afterparty 2026-27
echo ==========================================
echo   AFTERPARTY 2026-27 - LOCAL TEST SERVER
echo ==========================================
where py >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add Python to PATH" in the installer, then run this file again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment - first time only...
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
call .venv\Scripts\activate.bat
echo Installing needed parts - first time only, needs internet...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 goto failed
python setup_local.py
if errorlevel 1 goto failed
echo.
echo ------------------------------------------------------------
echo  Website is starting. KEEP THIS WINDOW OPEN.
echo  Attendee page : http://127.0.0.1:5000
echo  Organiser page: http://127.0.0.1:5000/admin
echo  (127.0.0.1 works only on this laptop. It is NOT the public website.)
echo  Press CTRL+C to stop.
echo ------------------------------------------------------------
start "" http://127.0.0.1:5000
python app.py
pause
exit /b 0
:failed
echo.
echo Something went wrong. Read the message above, or send a photo of it to your helper.
pause
exit /b 1
