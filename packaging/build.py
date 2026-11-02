"""Build the app for the system this runs on (PyInstaller can't cross-compile).

Windows: dist/GoProYTUploader/GoProYTUploader.exe. Mac: dist/GoProYTUploader.app.
Linux: dist/GoProYTUploader/GoProYTUploader. Run from the repo root: python packaging/build.py
Extra arguments go to PyInstaller, e.g. --distpath / --workpath to leave a running build alone.
"""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

icon = os.path.join("packaging", "icon.ico")
if not os.path.exists(icon):
    subprocess.run([sys.executable, os.path.join("packaging", "make_icon.py")], check=True)

args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", "GoProYTUploader",
        "--add-data", f"{os.path.join('gopro_uploader', 'ui')}{os.pathsep}ui",
        "--collect-data", "googleapiclient", "--collect-data", "tzdata",
        "--collect-all", "imageio_ffmpeg", "--collect-all", "webview"]
if sys.platform == "win32":  # a Mac .app wants .icns; without Pillow PyInstaller can't convert
    args += ["--icon", icon]
subprocess.run(args + sys.argv[1:] + [os.path.join("packaging", "entry.py")], check=True)
dist = sys.argv[sys.argv.index("--distpath") + 1] if "--distpath" in sys.argv else "dist"

# googleapiclient ships ~100 MB of discovery docs for every Google API; keep only YouTube
for f in glob.glob(os.path.join(dist, "**", "discovery_cache", "documents", "*.json"), recursive=True):
    if os.path.basename(f) != "youtube.v3.json":
        os.remove(f)
print("built", os.path.join(dist, "GoProYTUploader.app" if sys.platform == "darwin" else "GoProYTUploader"))
