"""Desktop window (pywebview) and the Python API exposed to the HTML UI."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading

import webview

from . import config, geocode, merge, preview, youtube
from .config import TITLE_FORMATS, Settings
from .planner import build_plan, detect_camera_offset
from .scanner import scan
from .store import Store, setup_logging

log = logging.getLogger("app")


def _resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


class Api:
    def __init__(self, log_file: str):
        self._window = None
        self._log_file = log_file
        self._settings = Settings.load()
        self._store = Store()
        self._recs = []
        self._overrides: dict[str, dict] = {}
        self._items: list[dict] = []
        self._yt = None
        self._cancel = False
        self._worker: threading.Thread | None = None
        self._preview: preview.PreviewServer | None = None

    # ------------------------------------------------------------ helpers
    def _emit(self, kind: str, **data):
        if self._window:
            payload = json.dumps({"type": kind, **data}, ensure_ascii=False, default=str)
            self._window.evaluate_js(f"window.onEvent({payload})")

    def _say(self, msg: str, level: str = "info"):
        getattr(log, "error" if level == "error" else "info")(msg)
        self._emit("log", msg=msg, level=level)

    def _place_lookup(self, lat, lon):
        return geocode.cached_place(lat, lon, self._settings.geocode_lang)

    def _replan(self):
        self._items = build_plan(self._recs, self._settings, self._overrides,
                                 store_lookup=self._store.lookup,
                                 place_lookup=self._place_lookup)
        off = detect_camera_offset(self._recs) if self._settings.camera_tz == "auto" else None
        return {"items": self._items,
                "camera_offset_hours": round(off.total_seconds() / 3600, 2) if off else None}

    # ---------------------------------------------------------- JS-facing
    def init(self):
        yt_ok = False
        try:
            self._yt = youtube.get_service(interactive=False)
            yt_ok = self._yt is not None
        except Exception as e:  # noqa: BLE001
            log.warning("silent sign-in failed: %s", e)
        channel = None
        if yt_ok:
            try:
                channel = youtube.channel_title(self._yt)
            except Exception as e:  # noqa: BLE001
                log.warning("channel lookup failed: %s", e)
        return {"settings": self._settings.to_dict(), "title_formats": TITLE_FORMATS,
                "has_secret": youtube.has_client_secret(), "channel": channel,
                "ffmpeg": merge.ffmpeg_exe() is not None, "app_dir": config.app_dir(),
                "log_file": self._log_file}

    def save_settings(self, values: dict):
        for k, v in values.items():
            if k in Settings.__dataclass_fields__:
                setattr(self._settings, k, v)
        pl = self._settings.playlist
        if pl and pl not in self._settings.recent_playlists:
            self._settings.recent_playlists = ([pl] + self._settings.recent_playlists)[:10]
        self._settings.save()
        return {**self._replan(), "settings": self._settings.to_dict()}

    def pick_folder(self):
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER,
                                            directory=self._settings.last_folder or "")
        return r[0] if r else None

    def scan(self, folder: str):
        if not os.path.isdir(folder):
            raise ValueError("資料夾不存在")
        self._settings.last_folder = folder
        self._settings.save()
        self._say(f"掃描 {folder} …")
        self._recs = scan(folder, progress=lambda i, n, name: self._emit("scan", i=i, n=n, name=name))
        self._overrides = {}
        res = self._replan()
        nfiles = sum(len(i["files"]) for i in self._items)
        self._say(f"找到 {nfiles} 個檔案，共 {len(self._items)} 段錄影")
        return res

    def edit(self, ids: list[str], fields: dict):
        for i in ids:
            ov = self._overrides.setdefault(i, {})
            for k, v in fields.items():
                if v is None:
                    ov.pop(k, None)
                else:
                    ov[k] = v
        return self._replan()

    def geocode_all(self):
        lang = self._settings.geocode_lang
        todo = {}
        for it in self._items:
            if it["lat"] is not None and not it["place"] and "place" not in self._overrides.get(it["id"], {}):
                todo.setdefault((round(it["lat"], 4), round(it["lon"], 4)), it)
        self._say(f"查詢 {len(todo)} 個地點的地名（OpenStreetMap，每秒一筆）…")
        for n, (lat, lon) in enumerate(todo, 1):
            try:
                place = geocode.reverse(lat, lon, lang)
                self._emit("geocode", i=n, n=len(todo), place=place)
            except Exception as e:  # noqa: BLE001
                self._say(f"地名查詢失敗 ({lat}, {lon})：{e}", "error")
        return self._replan()

    def preview_sources(self, item_id: str):
        """One playable URL per chapter, preferring the small .LRV proxy (H.264)."""
        item = next((i for i in self._items if i["id"] == item_id), None)
        if not item:
            return []
        if self._preview is None:
            self._preview = preview.PreviewServer()
        out = []
        for name, path in zip(item["files"], item["paths"]):
            lrv = preview.lrv_path(path)
            out.append({"name": name, "lrv": lrv is not None,
                        "url": self._preview.url_for(lrv or path)})
        return out

    def search_place(self, query: str, lat=None, lon=None):
        near = (lat, lon) if lat is not None and lon is not None else None
        return geocode.search(query, self._settings.geocode_lang, near)

    def pick_client_secret(self):
        r = self._window.create_file_dialog(webview.FileDialog.OPEN,
                                            file_types=("JSON (*.json)",))
        if not r:
            return False
        youtube.install_client_secret(r[0])
        self._say("已匯入 client_secret.json")
        return True

    def sign_in(self):
        self._yt = youtube.get_service(interactive=True)
        name = youtube.channel_title(self._yt)
        self._say(f"已登入 YouTube 頻道：{name}")
        return name

    def sign_out(self):
        youtube.sign_out()
        self._yt = None
        return True

    def open_path(self, which: str):
        target = {"data": config.app_dir(), "log": self._log_file,
                  "history": config.path("upload_history.csv")}.get(which, which)
        if os.path.exists(target):
            os.startfile(target)  # noqa: S606 - Windows only, user-initiated
            return True
        return False

    def start_upload(self):
        if self._worker and self._worker.is_alive():
            return False
        self._cancel = False
        self._worker = threading.Thread(target=self._upload_all, daemon=True)
        self._worker.start()
        return True

    def cancel(self):
        self._cancel = True
        self._say("取消中…目前這個區塊傳完就停止")
        return True

    # ---------------------------------------------------------- uploading
    def _upload_all(self):
        s = self._settings
        todo = [i for i in self._items if i["include"] and i["status"] != "uploaded"]
        if not todo:
            self._emit("done", msg="沒有要上傳的影片")
            return
        try:
            yt = self._yt or youtube.get_service(interactive=True)
            self._yt = yt
            playlist_id = youtube.find_or_create_playlist(yt, s.playlist) if s.playlist else None
        except Exception as e:  # noqa: BLE001
            log.exception("setup failed")
            self._emit("done", msg=f"無法開始上傳：{e}", level="error")
            return

        ok = 0
        for n, item in enumerate(todo, 1):
            if self._cancel:
                break
            self._say(f"[{n}/{len(todo)}] {item['title']}  ({' + '.join(item['files'])})")
            try:
                self._upload_item(yt, item, playlist_id)
                ok += 1
            except youtube.QuotaExceeded as e:
                self._set_status(item, "error", error=f"配額用完：{e}")
                self._say("YouTube 今日配額用完，請在台灣時間下午 3~4 點（太平洋時間午夜）後再繼續", "error")
                break
            except youtube.Cancelled:
                self._set_status(item, "pending")
                break
            except Exception as e:  # noqa: BLE001
                log.exception("upload failed")
                self._set_status(item, "error", error=str(e))
                self._say(f"失敗：{e}", "error")
        self._emit("done", msg=f"完成 {ok} / {len(todo)} 段" + ("（已取消）" if self._cancel else ""))

    def _set_status(self, item, status, **extra):
        item["status"] = status
        self._store.update(item, status=status, video_ids=item.get("video_ids", []), **extra)
        self._emit("item", id=item["id"], status=status, video_ids=item.get("video_ids", []),
                   error=extra.get("error"))

    def _sources(self, item):
        paths, title = item["paths"], item["title"]
        if len(paths) == 1:
            return [(paths[0], title, False)]
        if self._settings.merge_chapters and merge.ffmpeg_exe():
            self._emit("item", id=item["id"], status="merging")
            self._say(f"合併 {len(paths)} 個分段（無損）…")
            return [(merge.merge(paths, cancel=lambda: self._cancel), title, True)]
        return [(p, f"{title} ({i}/{len(paths)})", False) for i, p in enumerate(paths, 1)]

    def _upload_item(self, yt, item, playlist_id):
        s = self._settings
        prev = self._store.lookup(item) or {}
        done_ids = prev.get("video_ids", []) if prev.get("status") == "partial" else []
        item["video_ids"] = list(done_ids)
        sources = self._sources(item)
        for idx, (path, title, temp) in enumerate(sources):
            if idx < len(done_ids):
                continue
            self._emit("item", id=item["id"], status="uploading")
            body = youtube.video_body(title, item["description"], s.category_id, s.privacy,
                                      item["recording_date"], item["lat"], item["lon"],
                                      item["place"] or None)

            def progress(done, total, _idx=idx):
                self._emit("progress", id=item["id"], part=_idx + 1, parts=len(sources),
                           done=done, total=total)

            vid = youtube.upload(yt, path, body, progress=progress, cancel=lambda: self._cancel)
            item["video_ids"].append(vid)
            if playlist_id:
                youtube.add_to_playlist(yt, playlist_id, vid)
            self._store.append_history(item, vid, title, s.playlist)
            self._set_status(item, "partial" if idx + 1 < len(sources) else "uploaded")
            self._say(f"✓ https://youtu.be/{vid}")
            if temp and s.delete_merged:
                try:
                    os.remove(path)
                except OSError as e:
                    log.warning("could not delete merged file: %s", e)


def main():
    log_file = setup_logging()
    api = Api(log_file)
    window = webview.create_window("GoPro → YouTube 上傳工具", _resource("ui", "index.html"),
                                   js_api=api, width=1440, height=900, min_size=(1000, 650))
    api._window = window
    webview.start(http_server=True, debug="--debug" in sys.argv)
