@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run START_HERE.bat once first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --quiet opencv-python-headless numpy
python -m unittest discover -s tests -v
pause
