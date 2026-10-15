@echo off
rem Double-click to start. First run creates .venv and installs dependencies.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo First run: creating Python environment and installing packages...
    py -3 -m venv .venv || python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q --upgrade pip
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt
)
start "" .venv\Scripts\pythonw.exe -W ignore -m gopro_uploader
