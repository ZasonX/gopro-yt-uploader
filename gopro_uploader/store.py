"""Upload history: which recordings are done (state.json) plus a readable CSV log."""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import threading
from datetime import datetime

from . import config

_lock = threading.Lock()


def fingerprint(item: dict) -> str:
    """Identify a recording by first file name + total size, so moving the folder is fine."""
    return f"{item['files'][0].upper()}|{item['size']}"


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Edits:
    """The user's per-recording edits (title, time, place…) in edits.json, by fingerprint."""

    def __init__(self):
        self.file = config.path("edits.json")
        try:
            with open(self.file, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, fp: str) -> dict:
        return dict(self.data.get(fp, {}))

    def put(self, fp: str, fields: dict) -> None:
        with _lock:
            if fields:
                self.data[fp] = fields
            else:
                self.data.pop(fp, None)
            _write_json(self.file, self.data)


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
            _write_json(self.file, self.data)

    def patch(self, fp: str, **fields):
        """Change fields of one stored recording by fingerprint; None removes the field."""
        with _lock:
            rec = self.data.get(fp)
            if rec is None:
                return
            for k, v in fields.items():
                if v is None:
                    rec.pop(k, None)
                else:
                    rec[k] = v
            _write_json(self.file, self.data)

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


EXPORT_FILES = ("settings.json", "state.json", "edits.json", "upload_history.csv")
NEVER_EXPORT = ("token", "client_secret")  # sign-in secrets stay on this PC


def export_logs(dest: str, max_logs: int = 10) -> list[str]:
    """Zip the newest run logs plus the app's non-secret state into dest, for a bug report.

    Returns the names put in the zip. OAuth tokens and client_secret are never included.
    """
    import zipfile
    base = config.app_dir()
    logs_dir = os.path.join(base, "logs")
    logs = sorted((f for f in os.listdir(logs_dir) if f.endswith(".log")), reverse=True)[:max_logs] \
        if os.path.isdir(logs_dir) else []
    names = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in logs:
            z.write(os.path.join(logs_dir, f), f"logs/{f}")
            names.append(f"logs/{f}")
        for f in EXPORT_FILES:
            p = os.path.join(base, f)
            if os.path.exists(p) and not f.startswith(NEVER_EXPORT):
                z.write(p, f)
                names.append(f)
        z.writestr("README.txt", "GoPro → YouTube 上傳工具的紀錄匯出。\n"
                   "包含最近的執行紀錄、設定、上傳狀態、使用者修改、上傳紀錄 CSV。\n"
                   "不包含登入憑證（token / client_secret）。\n"
                   "注意：內容含有影片檔名、標題、地點和 YouTube 影片 ID，分享前請確認。\n")
    return names


def setup_logging() -> str:
    log_file = config.path("logs", datetime.now().strftime("%Y%m%d-%H%M%S.log"))
    # file for the record, stderr so a terminal launch shows scan/upload progress
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stderr is not None:  # None under pythonw.exe (run.bat)
        try:  # a cp950 pipe must not choke on Japanese place names
            sys.stderr.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # the OAuth helper logs the sign-in URL and the one-time auth code; keep those out of logs
    logging.getLogger("google_auth_oauthlib").setLevel(logging.WARNING)
    return log_file
