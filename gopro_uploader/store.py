"""Upload history: which recordings are done (state.json) plus a readable CSV log."""
from __future__ import annotations

import csv
import json
import logging
import os
import threading
from datetime import datetime

from . import config

_lock = threading.Lock()


def fingerprint(item: dict) -> str:
    """Identify a recording by first file name + total size, so moving the folder is fine."""
    return f"{item['files'][0].upper()}|{item['size']}"


class Store:
    def __init__(self):
        self.file = config.path("state.json")
        try:
            with open(self.file, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def lookup(self, item: dict):
        return self.data.get(fingerprint(item))

    def update(self, item: dict, **fields):
        with _lock:
            rec = self.data.setdefault(fingerprint(item), {})
            rec.update(fields, files=item["files"], updated=datetime.now().isoformat(timespec="seconds"))
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.file)

    def append_history(self, item: dict, video_id: str, part_title: str, playlist: str):
        p = config.path("upload_history.csv")
        new = not os.path.exists(p)
        with _lock, open(p, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["uploaded_at", "title", "files", "recording_date", "place",
                            "lat", "lon", "playlist", "video_id", "url"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), part_title,
                        " + ".join(item["files"]), item["recording_date"], item["place"],
                        item["lat"], item["lon"], playlist, video_id,
                        f"https://youtu.be/{video_id}"])


def setup_logging() -> str:
    log_file = config.path("logs", datetime.now().strftime("%Y%m%d-%H%M%S.log"))
    logging.basicConfig(level=logging.INFO, filename=log_file, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return log_file
