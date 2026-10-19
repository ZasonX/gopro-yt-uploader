@echo off
rem Build dist\GoProYTUploader\GoProYTUploader.exe (needs .venv from run.bat first).
cd /d "%~dp0"
.venv\Scripts\python.exe -m pip install -q -r requirements-build.txt || exit /b 1
if not exist packaging\icon.ico .venv\Scripts\python.exe packaging\make_icon.py
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed ^
    --name GoProYTUploader ^
    --icon packaging\icon.ico ^
    --add-data "gopro_uploader\ui;ui" ^
    --collect-data googleapiclient ^
    --collect-data tzdata ^
    --collect-all imageio_ffmpeg ^
    --collect-all webview ^
    packaging\entry.py || exit /b 1
rem googleapiclient ships ~100 MB of discovery docs for every Google API; keep only YouTube
for %%f in (dist\GoProYTUploader\_internal\googleapiclient\discovery_cache\documents\*.json) do (
    if /i not "%%~nxf"=="youtube.v3.json" del "%%f"
)
echo.
echo Built dist\GoProYTUploader\GoProYTUploader.exe
