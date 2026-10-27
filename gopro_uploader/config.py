"""User settings and app data paths (%APPDATA%\\GoProYTUploader)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


def app_dir() -> str:
    base = os.environ.get("GOPRO_YT_HOME") or os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), "GoProYTUploader")
    os.makedirs(base, exist_ok=True)
    return base


def path(*parts: str) -> str:
    p = os.path.join(app_dir(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


# The zone label (" JST") is appended by the planner when a recording's zone differs from home_tz.
TITLE_FORMATS = {
    "%Y-%m-%d %H:%M": "2026-03-15 14:32",
    "%Y年%m月%d日 %H:%M": "2026年03月15日 14:32",
    "%Y%m%d %H%M": "20260315 1432",
    "%Y/%m/%d %H:%M": "2026/03/15 14:32",
}


@dataclass
class Settings:
    # the YouTube playlist uploads go into: picked from the channel (or created) in the app.
    # Empty id = not chosen yet; uploads never create a playlist from a typed name.
    playlist: str = ""          # title, for display and upload_history.csv
    playlist_id: str = ""
    no_playlist: bool = False   # user chose "不加入播放清單"
    # fallback zone when a recording's own zone can't be read from its file times
    trip_tz: str = "Asia/Tokyo"
    # titles in this zone get no zone label; others get one (e.g. "JST")
    home_tz: str = "Asia/Taipei"
    # "auto" = derive the camera clock offset from GPS time; otherwise an IANA zone
    camera_tz: str = "auto"
    title_format: str = "%Y-%m-%d %H:%M"
    description_template: str = "{files}"
    category_id: str = "19"            # Travel & Events
    # Studio's 「中文（台灣）」 is zh-TW for both (read back from a video edited in Studio)
    title_language: str = "zh-TW"      # 標題和說明的語言 = snippet.defaultLanguage
    audio_language: str = "zh-TW"      # 影片語言 = snippet.defaultAudioLanguage
    privacy: str = "unlisted"          # unverified API projects get forced to private
    merge_chapters: bool = True
    delete_merged: bool = True
    # YouTube takes each upload at only ~3.5 MB/s (measured 2026-10-27), so send several at once (1-8)
    parallel_uploads: int = 3
    geocode_lang: str = "zh-TW,ja"
    last_folder: str = ""
    # last window position / size: {"x", "y", "width", "height", "maximized"}
    window: dict = field(default_factory=dict)
    # [{"place", "lat", "lon"}], newest first: quick picks under the place search
    recent_places: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(path("settings.json"), encoding="utf-8") as f:
                data = json.load(f)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            s = cls(**known)
        except (OSError, ValueError, TypeError):
            return cls()
        if s.title_format not in TITLE_FORMATS:  # e.g. the short-lived "… %Z" format
            s.title_format = cls.title_format
        return s

    def save(self) -> None:
        with open(path("settings.json"), "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    def to_dict(self) -> dict:
        return asdict(self)
