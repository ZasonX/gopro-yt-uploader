"""Desktop window (pywebview) and the Python API exposed to the HTML UI."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta

import webview

from . import config, geocode, merge, preview, youtube
from .config import TITLE_FORMATS, Settings
from .planner import build_plan, detect_camera_offset, retime_title
from .scanner import list_photos, scan
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
        self._photos: list[dict] = []
        self._overrides: dict[str, dict] = {}
        self._fps: dict[str, str] = {}  # recording id -> fingerprint used in edits.json
        self._items: list[dict] = []
        self._yt = None
        self._cancel = False
        self._worker: threading.Thread | None = None
        self._preview: preview.PreviewServer | None = None
        self._uploading_id: str | None = None
        self._told_signed_out = False

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
                "photos": self._photos,
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
        # the row shows what is on YouTube now (edited in Studio), not the local plan
        it["title"] = live[0]["title"]
        it["yt_place"] = live[0].get("place") or ""
        it["yt_has_location"] = live[0].get("lat") is not None

    def _sync_youtube(self):
        uploaded = [it for it in self._items if it["status"] in ("uploaded", "partial") and it.get("video_ids")]
        if not uploaded:
            return
        if not self._yt:
            self._say("沒有登入 YouTube，已上傳的影片先顯示上次從 YouTube 讀到的資料")
            return
        try:
            got = youtube.fetch_videos(self._yt, [v for it in uploaded for v in it["video_ids"]])
        except youtube.SignedOut as e:
            self._signed_out(e)
            return
        except Exception as e:  # noqa: BLE001 - offline etc.: keep showing the saved copy
            log.warning("could not read uploaded videos from YouTube: %s", e)
            self._say("無法從 YouTube 讀取已上傳影片的資料，先顯示上次讀到的資料", "error")
            return
        now = datetime.now().isoformat(timespec="minutes")
        for it in uploaded:  # None = not on YouTube any more
            self._store.update(it, yt={v: got.get(v) for v in it["video_ids"]}, yt_checked=now)

    def refresh_youtube(self):
        # re-plan first: self._items may be from mid-upload (an edit re-planned while the
        # worker uploaded), still saying "uploading" for a video that is now done
        self._replan()
        self._sync_youtube()
        return self._replan()

    # ------------------------------------------------------- sign-in state
    _EXPIRED_MSG = ("YouTube 登入已失效，請按右上角「重新登入」。"
                    "（Google Cloud 專案在「測試中」狀態時，登入每 7 天就會失效）")

    def _signed_out(self, err=None):
        """Any call found the saved sign-in no longer works: forget it and tell the user once."""
        if err:
            log.warning("signed out: %s", err)
        was = self._yt is not None or not self._told_signed_out
        self._yt = None
        youtube.sign_out()  # the saved token is dead; the next sign-in must open the browser
        if was:
            self._told_signed_out = True
            self._say(self._EXPIRED_MSG, "error")
            self._emit("signed_out")

    def check_login(self):
        """Called when the window regains focus (at most every few minutes): is the sign-in still good?

        Returns the channel name, or None when signed out / never signed in.
        """
        if not self._yt:
            return None
        try:
            return youtube.channel_title(self._yt)
        except youtube.SignedOut as e:
            self._signed_out(e)
        except Exception as e:  # noqa: BLE001 - offline: don't call it signed out
            log.warning("login check failed: %s", e)
        return None

    # ---------------------------------------------------------- JS-facing
    def init(self):
        yt_ok = False
        try:
            self._yt = youtube.get_service(interactive=False)
            yt_ok = self._yt is not None
        except youtube.SignedOut:
            self._expired_at_start = True
        except Exception as e:  # noqa: BLE001
            log.warning("silent sign-in failed: %s", e)
        channel = None
        if yt_ok:
            try:
                channel = youtube.channel_title(self._yt)
            except youtube.SignedOut as e:
                self._yt, yt_ok = None, False
                log.warning("saved sign-in rejected: %s", e)
                self._expired_at_start = True
            except Exception as e:  # noqa: BLE001
                log.warning("channel lookup failed: %s", e)
        expired = getattr(self, "_expired_at_start", False)
        if not yt_ok and not expired and youtube.has_saved_login():
            self._say("目前連不上 YouTube（網路？），已上傳影片先顯示上次讀到的資料", "error")
        if expired:
            youtube.sign_out()  # dead token: the next sign-in must open the browser
            self._told_signed_out = True
            self._say(self._EXPIRED_MSG, "error")
        return {"settings": self._settings.to_dict(), "title_formats": TITLE_FORMATS,
                "login_expired": expired,
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
        """A working service, opening the browser sign-in when needed."""
        if not self._yt:
            self._say("請在跳出的瀏覽器頁面登入 YouTube…")
            self._yt = youtube.get_service(interactive=True)
            self._told_signed_out = False
            self._emit("channel", name=youtube.channel_title(self._yt))
        return self._yt

    def _with_sign_in(self, fn):
        """Run fn(yt); if the sign-in turns out to be dead, sign in again and retry once."""
        try:
            return fn(self._signed_in())
        except youtube.SignedOut as e:
            self._signed_out(e)
            return fn(self._signed_in())

    def list_playlists(self):
        return self._with_sign_in(youtube.list_playlists)

    def create_playlist(self, title: str, privacy: str):
        title = title.strip()
        if not title:
            raise ValueError("請輸入播放清單名稱")
        p = self._with_sign_in(lambda yt: youtube.create_playlist(yt, title, privacy))
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
        self._photos = list_photos(folder)  # they use up GoPro file numbers: explains gaps
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

    def set_dates(self, ids: list[str], date: str):
        """Set the day of several recordings at once; one that has a real time keeps it."""
        by_id = {it["id"]: it for it in self._items}
        for i in ids:
            it = by_id.get(i)
            if not it or self._locked(i):
                continue
            lt = f"{date}T{it['local'][11:16]}" if it["time_known"] else date
            self.edit([i], {"local_time": lt})
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
            new = retime_title(ov["title"], datetime.fromisoformat(it["local"]), it["time_known"])
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
        rec = next((r for r in self._recs if r.key == item_id), None)
        durs = [c.info.duration if c.info else 0 for c in rec.clips] if rec else []
        out, start = [], 0.0
        for n, (name, path) in enumerate(zip(item["files"], item["paths"])):
            lrv = preview.lrv_path(path)
            # "start": seconds into the whole recording where this chapter begins
            # (a clock seen in chapter 2 at 0:30 is start + 30 s into the recording)
            out.append({"name": name, "lrv": lrv is not None, "start": start,
                        "url": self._preview.url_for(lrv or path)})
            start += durs[n] if n < len(durs) else 0
        return out

    def time_from_clock(self, item_id: str, position: float, clock: str):
        """A clock reading "HH:MM" seen `position` seconds into the recording -> its start time.

        Sets local_time (date from the recording's current / guessed date, minus a day if
        the start falls before midnight) and returns the new plan.
        """
        it = next((i for i in self._items if i["id"] == item_id), None)
        if not it or self._locked(item_id):
            return self._replan()
        hh, mm = (int(x) for x in clock.split(":"))
        seen = datetime.fromisoformat(it["local"][:10]).replace(hour=hh, minute=mm)
        start = seen - timedelta(seconds=round(position))
        start = (start + timedelta(seconds=30)).replace(second=0)  # the clock only shows minutes
        m, s = divmod(int(position), 60)
        self._say(f"{' + '.join(it['files'])}：{m}:{s:02d} 看到時鐘 {clock} → 推算開始時間 {start:%H:%M}")
        return self.edit([item_id], {"local_time": start.strftime("%Y-%m-%dT%H:%M")})

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
        youtube.sign_out()  # 登入 / 重新登入 / 切換帳號 always go through the browser
        self._yt = youtube.get_service(interactive=True)
        self._told_signed_out = False
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

    def _scanned_file(self, item_id: str, index: int = 0) -> str | None:
        """Only files of a scanned recording can be opened from the page."""
        it = next((i for i in self._items if i["id"] == item_id), None)
        if not it or not (0 <= index < len(it["paths"])):
            return None
        p = it["paths"][index]
        return p if os.path.isfile(p) else None

    def reveal_file(self, item_id: str, index: int = 0):
        """Open Explorer at the file's folder with the file selected."""
        p = self._scanned_file(item_id, index)
        if not p:
            return False
        subprocess.Popen(["explorer", f"/select,{os.path.normpath(p)}"])  # noqa: S603,S607
        return True

    def open_original(self, item_id: str, index: int = 0):
        """Play the original (full-resolution) file in the default player."""
        p = self._scanned_file(item_id, index)
        if not p:
            return False
        os.startfile(p)  # noqa: S606 - Windows only, user-initiated
        return True

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
            if self._yt:
                self.check_login()  # a dead sign-in is reported and dropped here…
            yt = self._signed_in()  # …and the browser sign-in opens before any upload starts
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
                try:
                    self._upload_item(yt, item, playlist_id)
                except youtube.SignedOut as e:
                    # expired mid-batch: sign in again in the browser, then redo this recording
                    self._signed_out(e)
                    self._say("請在跳出的瀏覽器頁面重新登入，登入後會從這一段接著上傳")
                    yt = self._signed_in()
                    self._upload_item(yt, item, playlist_id)
                ok += 1
            except youtube.SignedOut as e:
                self._signed_out(e)
                self._set_status(item, "error", error="YouTube 登入失效")
                break
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
        if status == "uploaded":  # done: drop the user's tick so the row shows as not queued
            self._overrides.get(item["id"], {}).pop("include", None)
            item["include"] = False
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


def window_geometry(saved: dict, screens: list) -> dict:
    """Where to open: the last position if it is still on a connected screen, else centred.

    screens: [(x, y, width, height)]; the first one is the primary screen.
    """
    w = max(int(saved.get("width") or 1440), 1000)
    h = max(int(saved.get("height") or 900), 650)
    x, y = saved.get("x"), saved.get("y")
    if x is not None and y is not None:
        cx, cy = x + w // 2, y + 40  # the title bar must be reachable
        if any(sx <= cx < sx + sw and sy <= cy < sy + sh for sx, sy, sw, sh in screens):
            return {"x": int(x), "y": int(y), "width": w, "height": h,
                    "maximized": bool(saved.get("maximized"))}
    sx, sy, sw, sh = screens[0] if screens else (0, 0, 1920, 1080)
    w, h = min(w, sw), min(h, sh)
    return {"x": sx + (sw - w) // 2, "y": sy + (sh - h) // 2, "width": w, "height": h,
            "maximized": bool(saved.get("maximized"))}


def _remember_window(window, settings: Settings):
    geo = dict(settings.window)

    def moved(x, y):
        if not geo.get("maximized"):
            geo.update(x=x, y=y)

    def resized(width, height):
        if not geo.get("maximized"):
            geo.update(width=width, height=height)

    def closing():
        settings.window = geo
        settings.save()

    window.events.moved += moved
    window.events.resized += resized
    window.events.maximized += lambda: geo.update(maximized=True)
    window.events.restored += lambda: geo.update(maximized=False)
    window.events.closing += closing


def main():
    log_file = setup_logging()
    api = Api(log_file)
    try:
        screens = [(s.x, s.y, s.width, s.height) for s in webview.screens]
    except Exception:  # noqa: BLE001 - no screen info: just centre on a default size
        screens = []
    geo = window_geometry(api._settings.window, screens)
    window = webview.create_window("GoPro → YouTube 上傳工具", _resource("ui", "index.html"),
                                   js_api=api, width=geo["width"], height=geo["height"],
                                   x=geo["x"], y=geo["y"], maximized=geo["maximized"],
                                   min_size=(1000, 650))
    api._window = window
    _remember_window(window, api._settings)
    webview.start(http_server=True, debug="--debug" in sys.argv)
