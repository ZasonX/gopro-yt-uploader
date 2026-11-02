@echo off
rem Build dist\GoProYTUploader\GoProYTUploader.exe (needs .venv from run.bat first).
cd /d "%~dp0"
.venv\Scripts\python.exe -m pip install -q -r requirements-build.txt || exit /b 1
.venv\Scripts\python.exe packaging\build.py || exit /b 1
