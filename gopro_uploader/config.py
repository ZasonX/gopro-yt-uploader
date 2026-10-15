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


TITLE_FORMATS = {
    "%Y-%m-%d %H:%M": "2026-03-15 14:32",
    "%Y年%m月%d日 %H:%M": "2026年03月15日 14:32",
    "%Y%m%d %H%M": "20260315 1432",
    "%Y/%m/%d %H:%M": "2026/03/15 14:32",
}


@dataclass
class Settings:
    playlist: str = "2025 日本"
    trip_tz: str = "Asia/Tokyo"
    # "auto" = derive the camera clock offset from GPS time; otherwise an IANA zone
    camera_tz: str = "auto"
    title_format: str = "%Y-%m-%d %H:%M"
    description_template: str = "{files}"
    category_id: str = "19"            # Travel & Events
    privacy: str = "unlisted"          # unverified API projects get forced to private
    merge_chapters: bool = True
    delete_merged: bool = True
    geocode_lang: str = "zh-TW,ja"
    last_folder: str = ""
    recent_playlists: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(path("settings.json"), encoding="utf-8") as f:
                data = json.load(f)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        with open(path("settings.json"), "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    def to_dict(self) -> dict:
        return asdict(self)
