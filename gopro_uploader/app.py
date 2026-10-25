"""Desktop window (pywebview) and the Python API exposed to the HTML UI."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime

import webview

from . import config, geocode, merge, preview, youtube
from .config import TITLE_FORMATS, Settings
from .planner import build_plan, detect_camera_offset, retime_title
from .scanner import scan
from .store import Edits, Store, fingerprint, setup_logging
from .store import export_logs as store_export_logs

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
        self._edits = Edits()
        self._recs = []
        self._overrides: dict[str, dict] = {}
        self._fps: dict[str, str] = {}  # recording id -> fingerprint used in edits.json
        self._items: list[dict] = []
        self._yt = None
        self._cancel = False
        self._worker: threading.Thread | None = None
        self._preview: preview.PreviewServer | None = None
        self._uploading_id: str | None = None

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
        for it in self._items:  # "uploading" lives only in memory, not in state.json
            if it["id"] == self._uploading_id and it["status"] != "uploaded":
                it["status"] = "uploading"
            self._attach_youtube(it)
        off = detect_camera_offset(self._recs) if self._settings.camera_tz == "auto" else None
        return {"items": self._items, "recent_places": self._settings.recent_places,
                "camera_offset_hours": round(off.total_seconds() / 3600, 2) if off else None}

    # ------------------------------------------------- YouTube's current data
    def _attach_youtube(self, it):
        """Uploaded rows show the last copy read from YouTube (titles edited in Studio, etc.).

        That copy is saved in state.json, so it is still shown after a restart when the
        sign-in has expired or there is no network; yt_checked says when it was read.
        """
        if it["status"] not in ("uploaded", "partial") or not it.get("video_ids"):
            return
        vids = it["video_ids"]
        rec = self._store.lookup(it) or {}
        saved = rec.get("yt") or {}
        if not all(v in saved for v in vids):
            return  # never read from YouTube yet: only the local upload record exists
        it["yt_checked"] = rec.get("yt_checked")
        it["yt"] = [{"id": v, **saved[v]} if saved[v] else {"id": v, "missing": True} for v in vids]
        live = [y for y in it["yt"] if not y.get("missing")]
        if not live:
            # deleted on YouTube: let it be ticked and uploaded again
            it["status"] = "missing"
            return
        it["title"] = live[0]["title"]

    def _sync_youtube(self):
        uploaded = [it for it in self._items if it["status"] in ("uploaded", "partial") and it.get("video_ids")]
        if not uploaded:
            return
        if not self._yt:
            self._say("沒有登入 YouTube，已上傳的影片先顯示上次從 YouTube 讀到的資料")
            return
        try:
            got = youtube.fetch_videos(self._yt, [v for it in uploaded for v in it["video_ids"]])
        except Exception as e:  # noqa: BLE001 - offline etc.: keep showing the saved copy
            log.warning("could not read uploaded videos from YouTube: %s", e)
            self._say("無法從 YouTube 讀取已上傳影片的資料，先顯示上次讀到的資料", "error")
            return
        now = datetime.now().isoformat(timespec="minutes")
        for it in uploaded:  # None = not on YouTube any more
            self._store.update(it, yt={v: got.get(v) for v in it["video_ids"]}, yt_checked=now)

    def refresh_youtube(self):
        self._sync_youtube()
        return self._replan()

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
        if not yt_ok and youtube.has_saved_login():
            self._say("YouTube 登入已過期，請按右上角「登入」重新登入。"
                      "（Google Cloud 專案還在「測試中」時，登入每 7 天就會過期）", "error")
        return {"settings": self._settings.to_dict(), "title_formats": TITLE_FORMATS,
                "login_expired": not yt_ok and youtube.has_saved_login(),
                "has_secret": youtube.has_client_secret(), "channel": channel,
                "ffmpeg": merge.ffmpeg_exe() is not None, "app_dir": config.app_dir(),
                "log_file": self._log_file}

    def save_settings(self, values: dict):
        for k, v in values.items():
            if k in Settings.__dataclass_fields__:
                setattr(self._settings, k, v)
        self._settings.save()
        return {**self._replan(), "settings": self._settings.to_dict()}

    # ---------------------------------------------------------- playlists
    def _signed_in(self):
        if not self._yt:
            self._yt = youtube.get_service(interactive=True)
            self._emit("channel", name=youtube.channel_title(self._yt))
        return self._yt

    def list_playlists(self):
        return youtube.list_playlists(self._signed_in())

    def create_playlist(self, title: str, privacy: str):
        title = title.strip()
        if not title:
            raise ValueError("請輸入播放清單名稱")
        p = youtube.create_playlist(self._signed_in(), title, privacy)
        self._say(f"已在 YouTube 建立播放清單「{p['title']}」（{privacy}）")
        return p

    def choose_playlist(self, playlist_id: str | None, title: str | None):
        """None = upload without adding to any playlist."""
        s = self._settings
        s.playlist_id, s.playlist, s.no_playlist = playlist_id or "", title or "", not playlist_id
        s.save()
        self._say(f"上傳後加入播放清單：{title}" if playlist_id else "上傳後不加入任何播放清單")
        return s.to_dict()

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
        self._fps = {r.key: fingerprint({"files": [c.name for c in r.clips],
                                         "size": sum(c.size for c in r.clips)})
                     for r in self._recs}
        self._overrides = {k: ov for k, fp in self._fps.items() if (ov := self._edits.get(fp))}
        self._replan()
        self._sync_youtube()
        res = self._replan()
        nfiles = sum(len(i["files"]) for i in self._items)
        self._say(f"找到 {nfiles} 個檔案，共 {len(self._items)} 段錄影")
        return res

    _FIELD_NAMES = {"title": "標題", "local_time": "拍攝時間", "tz": "時區", "place": "地點名稱",
                    "lat": "座標", "include": "上傳勾選"}

    def _log_edit(self, item_id: str, fields: dict):
        it = next((i for i in self._items if i["id"] == item_id), None)
        name = " + ".join(it["files"]) if it else item_id
        parts = []
        for k, v in fields.items():
            if k == "lon":
                continue  # shown together with lat
            if k == "lat" and v is not None:
                v = f"{v:.5f}, {fields.get('lon', 0):.5f}"
            parts.append(f"{self._FIELD_NAMES.get(k, k)} → {'（還原）' if v is None else v}")
        self._say(f"修改 {name}：" + "，".join(parts))

    def _locked(self, item_id: str) -> bool:
        """Uploading or uploaded: its data is already on YouTube, edits here would do nothing."""
        if item_id == self._uploading_id:
            return True
        it = next((i for i in self._items if i["id"] == item_id), None)
        return bool(it and it["status"] in ("uploaded", "partial"))

    def edit(self, ids: list[str], fields: dict):
        if any(k != "include" for k in fields):
            skipped = [i for i in ids if self._locked(i)]
            if skipped:
                self._say(f"略過 {len(skipped)} 段上傳中或已上傳的錄影；已上傳的請到 YouTube Studio 修改")
            ids = [i for i in ids if i not in skipped]
        if ids and fields.get("place") and fields.get("lat") is not None and fields.get("lon") is not None:
            self._remember_place(fields["place"], fields["lat"], fields["lon"])
        for i in ids:
            if any(k != "include" for k in fields):  # ticks are too chatty to log
                self._log_edit(i, fields)
            ov = self._overrides.setdefault(i, {})
            for k, v in fields.items():
                if v is None:
                    ov.pop(k, None)
                else:
                    ov[k] = v
            self._save_edits(i)
        if ids and ("local_time" in fields or "tz" in fields):
            self._retime_titles(ids)
        return self._replan()

    def fix_title_dates(self, ids: list[str]):
        self._retime_titles([i for i in ids if not self._locked(i)])
        return self._replan()

    def _save_edits(self, item_id: str):
        if item_id in self._fps:  # not "include": a remembered tick could re-upload a finished video
            ov = self._overrides.get(item_id, {})
            self._edits.put(self._fps[item_id], {k: v for k, v in ov.items() if k != "include"})

    def _retime_titles(self, ids: list[str]):
        """A new time also fixes the date / time at the start of a hand-typed title."""
        self._replan()
        by_id = {it["id"]: it for it in self._items}
        for i in ids:
            ov, it = self._overrides.get(i, {}), by_id.get(i)
            if not it or "title" not in ov:
                continue
            new = retime_title(ov["title"], datetime.fromisoformat(it["local"]))
            if new != ov["title"]:
                ov["title"] = new
                self._save_edits(i)
                self._say(f"標題的時間跟著改成：{new}")

    def _remember_place(self, place: str, lat: float, lon: float):
        rest = [p for p in self._settings.recent_places if p.get("place") != place]
        self._settings.recent_places = ([{"place": place, "lat": lat, "lon": lon}] + rest)[:10]
        self._settings.save()

    def reset_edits(self, ids: list[str]):
        """Forget every edit of these recordings (the upload tick stays as it is)."""
        for i in ids:
            ov = self._overrides.get(i, {})
            keys = {k: None for k in ov if k != "include"}
            if keys:
                self.edit([i], keys)
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

    def export_logs(self):
        name = f"GoProYTUploader-紀錄-{datetime.now():%Y%m%d-%H%M}.zip"
        r = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name,
                                            file_types=("Zip 壓縮檔 (*.zip)",))
        if not r:
            return None
        dest = r if isinstance(r, str) else r[0]
        if not dest.lower().endswith(".zip"):
            dest += ".zip"
        files = store_export_logs(dest)
        self._say(f"已匯出紀錄（{len(files)} 個檔案，不含登入憑證）：{dest}")
        return dest

    def open_path(self, which: str):
        target = {"data": config.app_dir(), "log": self._log_file,
                  "history": config.path("upload_history.csv")}.get(which)
        if target and os.path.exists(target):
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
        if not s.playlist_id and not s.no_playlist:
            self._emit("done", msg="請先選擇播放清單（上方「播放清單」），或選「不加入播放清單」", level="error")
            return
        try:
            if not self._yt:
                self._say("需要登入 YouTube，請在跳出的瀏覽器頁面登入…")
            yt = self._signed_in()
            playlist_id = s.playlist_id or None
        except Exception as e:  # noqa: BLE001
            log.exception("setup failed")
            self._emit("done", msg=f"無法開始上傳：{e}", level="error")
            return

        ok = 0
        for n, queued in enumerate(todo, 1):
            if self._cancel:
                break
            # edits made while earlier videos were uploading re-plan self._items;
            # take this recording's latest version, not the snapshot from the start
            item = next((i for i in self._items if i["id"] == queued["id"]), queued)
            self._uploading_id = item["id"]
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
        self._uploading_id = None
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
                                      item["recording_date"], s.title_language, s.audio_language)

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
