"""Find GoPro videos in a folder and group chapter files into recordings.

GoPro naming:
  HERO6+   GX010123.MP4, GX020123.MP4 ...  (G + H/X codec, 2-digit chapter, 4-digit number)
  HERO5-   GOPR0123.MP4 (first chapter), GP010123.MP4, GP020123.MP4 ...
.LRV (low-res proxy) and .THM (thumbnail) files are ignored.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from .mp4meta import Mp4Info, read_mp4

VIDEO_EXTS = {".mp4", ".mov"}
_NEW = re.compile(r"^G([HX])(\d{2})(\d{4})$", re.I)
_OLD_FIRST = re.compile(r"^GOPR(\d{4})$", re.I)
_OLD_CHAP = re.compile(r"^GP(\d{2})(\d{4})$", re.I)


@dataclass
class Clip:
    path: str
    size: int
    mtime: float
    info: Mp4Info | None = None
    error: str | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class Recording:
    key: str
    clips: list[Clip] = field(default_factory=list)


def group_key(filename: str) -> tuple[str, int]:
    """Return (recording key, chapter index) for a file name."""
    stem, _ = os.path.splitext(filename)
    if m := _NEW.match(stem):
        return f"G{m[1].upper()}{m[3]}", int(m[2])
    if m := _OLD_FIRST.match(stem):
        return f"GOPR{m[1]}", 0
    if m := _OLD_CHAP.match(stem):
        return f"GOPR{m[2]}", int(m[1])
    return stem, 0


def list_videos(folder: str, recursive: bool = True) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_merged"))]
        for fn in files:
            if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                out.append(os.path.join(root, fn))
        if not recursive:
            break
    return sorted(out)


def scan(folder: str, progress=None, recursive: bool = True) -> list[Recording]:
    paths = list_videos(folder, recursive)
    groups: dict[str, list[tuple[int, Clip]]] = {}
    for i, p in enumerate(paths):
        st = os.stat(p)
        clip = Clip(p, st.st_size, st.st_mtime)
        try:
            clip.info = read_mp4(p)
        except Exception as e:  # noqa: BLE001 - report and keep going
            clip.error = str(e)
        key, chap = group_key(os.path.basename(p))
        key = os.path.join(os.path.dirname(p), key)
        groups.setdefault(key, []).append((chap, clip))
        if progress:
            progress(i + 1, len(paths), os.path.basename(p))

    recs = [Recording(k, [c for _, c in sorted(v, key=lambda x: x[0])])
            for k, v in groups.items()]
    recs.sort(key=lambda r: _start_time(r) or datetime.max)
    return recs


def _start_time(rec: Recording):
    c = rec.clips[0]
    if c.info and c.info.camera_time:
        return c.info.camera_time
    return datetime.fromtimestamp(c.mtime)
