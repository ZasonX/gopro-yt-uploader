"""Losslessly join GoPro chapter files with ffmpeg (stream copy, no re-encode)."""
from __future__ import annotations

import os
import shutil
import subprocess


def ffmpeg_exe() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def merged_path(paths: list[str]) -> str:
    folder = os.path.join(os.path.dirname(paths[0]), "_merged")
    stem = os.path.splitext(os.path.basename(paths[0]))[0]
    return os.path.join(folder, f"{stem}_merged.mp4")


def merge(paths: list[str], cancel=None) -> str:
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("找不到 ffmpeg，無法合併分段")
    out = merged_path(paths)
    total = sum(os.path.getsize(p) for p in paths)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out) and os.path.getsize(out) > total * 0.95:
        return out  # already merged in an earlier run
    free = shutil.disk_usage(os.path.dirname(out)).free
    if free < total * 1.05:
        raise RuntimeError(f"磁碟空間不足：合併需要約 {total / 1e9:.1f} GB")

    list_file = out + ".txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '{}'\n".format(os.path.abspath(p).replace("'", r"'\''")))
    tmp = out + ".part.mp4"
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", list_file, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", tmp]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    while proc.poll() is None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if cancel and cancel():
                proc.kill()
                raise RuntimeError("已取消")
    err = proc.stderr.read().decode("utf-8", "ignore")
    os.remove(list_file)
    if proc.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(f"ffmpeg 合併失敗：{err.strip()[-500:]}")
    os.replace(tmp, out)
    return out
