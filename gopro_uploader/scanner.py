"""Find GoPro videos in a folder and group chapter files into recordings.

GoPro naming:
  HERO6+   GX010123.MP4, GX020123.MP4 ...  (G + H/X codec, 2-digit chapter, 4-digit number)
  HERO5-   GOPR0123.MP4 (first chapter), GP010123.MP4, GP020123.MP4 ...
.LRV (low-res proxy) and .THM (thumbnail) files are ignored.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .mp4meta import GpsPoint, Mp4Info, read_mp4

log = logging.getLogger(__name__)

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


class ScanCache:
    """Parsed Mp4Info per file in app data (scan_cache.json), keyed by full path.

    An entry is only used when size and mtime still match, so a changed file
    is parsed again. A missing or broken cache file is ignored and rebuilt.
    """
    VERSION = 1
    SAVE_EVERY = 25   # keep progress if a long first scan is interrupted

    def __init__(self):
        self.file = config.path("scan_cache.json")
        self.entries: dict[str, dict] = {}
        self._dirty = 0
        try:
            with open(self.file, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") == self.VERSION and isinstance(data.get("entries"), dict):
                self.entries = data["entries"]
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def get(self, path: str, size: int, mtime: float) -> Mp4Info | None:
        e = self.entries.get(self._key(path))
        if not isinstance(e, dict) or e.get("size") != size or e.get("mtime") != mtime:
            return None
        try:
            return _info_from_json(e["info"])
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, path: str, size: int, mtime: float, info: Mp4Info) -> None:
        self.entries[self._key(path)] = {"size": size, "mtime": mtime, "info": _info_to_json(info)}
        self._dirty += 1
        if self._dirty >= self.SAVE_EVERY:
            self.save()

    def save(self) -> None:
        if not self._dirty:
            return
        tmp = self.file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": self.VERSION, "entries": self.entries}, f,
                          ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.file)
            self._dirty = 0
        except OSError as e:
            log.warning("無法寫入掃描快取 %s: %s", self.file, e)


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def _info_to_json(info: Mp4Info) -> dict:
    return {
        "camera_time": info.camera_time.isoformat() if info.camera_time else None,
        "duration": info.duration,
        "gps": [[g.t, g.lat, g.lon, g.utc.isoformat() if g.utc else None] for g in info.gps],
    }


def _info_from_json(d: dict) -> Mp4Info:
    return Mp4Info(_dt(d["camera_time"]), float(d["duration"]),
                   [GpsPoint(float(t), float(lat), float(lon), _dt(utc))
                    for t, lat, lon, utc in d["gps"]])


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
    cache = ScanCache()
    groups: dict[str, list[tuple[int, Clip]]] = {}
    hits = 0
    for i, p in enumerate(paths):
        st = os.stat(p)
        clip = Clip(p, st.st_size, st.st_mtime)
        clip.info = cache.get(p, st.st_size, st.st_mtime)
        if clip.info is not None:
            hits += 1
        else:
            try:
                clip.info = read_mp4(p)
                cache.put(p, st.st_size, st.st_mtime, clip.info)
            except Exception as e:  # noqa: BLE001 - report and keep going
                clip.error = str(e)
                log.warning("無法解析 %s: %s", p, e)
        key, chap = group_key(os.path.basename(p))
        key = os.path.join(os.path.dirname(p), key)
        groups.setdefault(key, []).append((chap, clip))
        if progress:
            progress(i + 1, len(paths), os.path.basename(p))
    cache.save()
    log.info("掃描 %s：%d 個檔案，快取命中 %d，重新解析 %d",
             folder, len(paths), hits, len(paths) - hits)

    recs = [Recording(k, [c for _, c in sorted(v, key=lambda x: x[0])])
            for k, v in groups.items()]
    recs.sort(key=lambda r: _start_time(r) or datetime.max)
    return recs


def _start_time(rec: Recording):
    c = rec.clips[0]
    if c.info and c.info.camera_time:
        return c.info.camera_time
    return datetime.fromtimestamp(c.mtime)
