"""Desktop window (pywebview) and the Python API exposed to the HTML UI."""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import webview

from . import config, geocode, merge, preview, taskbar, youtube
from .config import TITLE_FORMATS, Settings
from .planner import build_plan, detect_camera_offset, retime_title
from .scanner import list_photos, scan
from .store import Edits, Store, fingerprint, setup_logging
from .store import export_logs as store_export_logs

log = logging.getLogger("app")

WINDOW_TITLE = "GoPro → YouTube 上傳工具"
MAX_PARALLEL = 8  # HDD reads and YouTube's per-account limits are unknown beyond this
MAX_PARKED = 6  # uploads that may wait for their last chunk on top of settings.parallel_uploads
QUOTA_POLL = 10  # seconds between checks while waiting for the quota to reset
RESUME_FOR = timedelta(days=7)  # YouTube keeps a stopped upload session about a week
CLOSE_WAIT = 20  # seconds to let uploads stop and save their place when the window closes


def _resume_sig(path: str, size: int, idx: int, body: dict) -> str:
    """Same file and same title / description / date as the stopped upload: it can go on."""
    return hashlib.sha1(json.dumps([path, size, idx, body], sort_keys=True,
                                   ensure_ascii=False).encode()).hexdigest()


def quota_reset(now: datetime | None = None) -> datetime:
    """Local time to try again after a quota stop: YouTube resets at Pacific midnight (+5 min slack).

    Right after a reset that didn't help (the next midnight is ~a day away), try every 30 min instead.
    """
    now = now or datetime.now()
    pacific = now.astimezone(ZoneInfo("America/Los_Angeles"))
    midnight = (pacific + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    at = midnight.astimezone().replace(tzinfo=None) + timedelta(minutes=5)
    return now + timedelta(minutes=30) if at - now > timedelta(hours=23) else at


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
        self._stop_after = False  # finish the current video, then stop
        self._worker: threading.Thread | None = None
        self._preview: preview.PreviewServer | None = None
        self._uploading_ids: set[str] = set()  # several upload at once (settings.parallel_uploads)
        self._parked: set[str] = set()  # sent all but the last chunk, waiting for the one before
        self._taskbar = taskbar.Taskbar(WINDOW_TITLE)
        self._sent: dict[str, float] = {}  # taskbar: recording id -> bytes sent; plus the batch total
        self._batch_bytes = 1
        self._told_signed_out = False
        self._quota_until: datetime | None = None  # while the batch waits for the quota reset
        self._closed = False  # window gone: nothing to send events to

    # ------------------------------------------------------------ helpers
    def _emit(self, kind: str, **data):
        if self._window and not self._closed:
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
            if it["id"] in self._uploading_ids and it["status"] != "uploaded":
                it["status"] = "uploading"
            self._attach_youtube(it)
            it["resume"] = self._resume_info(it) if it["status"] not in ("uploaded", "uploading") else None
        off = detect_camera_offset(self._recs) if self._settings.camera_tz == "auto" else None
        return {"items": self._items, "recent_places": self._settings.recent_places,
                "photos": self._photos,
                "camera_offset_hours": round(off.total_seconds() / 3600, 2) if off else None}

    def _upload_body(self, item, title):
        s = self._settings
        return youtube.video_body(title, item["description"], s.category_id, s.privacy,
                                  item["recording_date"], s.title_language, s.audio_language)

    def _resume_info(self, it):
        """A stopped upload YouTube still holds: how far it got, until when, and whether the
        next start can go on from there (an edited title or description starts over)."""
        r = (self._store.lookup(it) or {}).get("resume") or {}
        if not r.get("uri"):
            return None
        part, parts = r.get("part") or 1, r.get("parts") or 1
        pct = 100 * ((part - 1) + r["done"] / r["total"]) / parts if r.get("total") else None
        try:
            until = datetime.fromisoformat(r["at"]) + RESUME_FOR
        except (KeyError, TypeError, ValueError):
            until = None
        paths = it["paths"]
        if len(paths) == 1:
            path, title = paths[0], it["title"]
        elif self._settings.merge_chapters and merge.ffmpeg_exe():
            path, title = merge.merged_path(paths), it["title"]
        else:
            path, title = paths[part - 1], f"{it['title']} ({part}/{len(paths)})"
        try:
            same = r.get("sig") == _resume_sig(path, os.path.getsize(path), part - 1, self._upload_body(it, title))
        except (OSError, IndexError):
            same = None  # merged file gone or a part missing: can't tell
        return {"pct": pct, "until": until.isoformat(timespec="minutes") if until else None,
                "expired": bool(until and datetime.now() > until), "changed": same is False}

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
        # the row shows what is on YouTube now (edited in Studio), not the local plan,
        # except fields edited here after the upload and not yet sent (yt_pending)
        pending = rec.get("yt_dirty") or []
        it["yt_pending"] = pending
        if "title" not in pending:
            it["title"] = live[0]["title"]
        if "description" not in pending:
            it["description"] = live[0].get("description", "")
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

    def push_to_youtube(self):
        """Send titles / descriptions edited here after upload to YouTube, in one go."""
        self._replan()
        todo = [it for it in self._items if it.get("yt_pending") and it.get("video_ids")]
        if not todo:
            return self._replan()
        self._say(f"更新 {len(todo)} 支影片的標題 / 說明到 YouTube …")
        done = 0
        for it in todo:
            fields = set(it["yt_pending"])
            vids = it["video_ids"]
            try:
                for n, vid in enumerate(vids, 1):
                    title = it["title"] + (f" ({n}/{len(vids)})" if len(vids) > 1 else "")
                    self._with_sign_in(lambda yt, v=vid, t=title: youtube.update_snippet(
                        yt, v, title=t if "title" in fields else None,
                        description=it["description"] if "description" in fields else None))
            except youtube.QuotaExceeded:
                self._say("YouTube 今日配額用完，剩下的明天再更新", "error")
                break
            except Exception as e:  # noqa: BLE001
                log.exception("update failed")
                self._say(f"更新失敗（{' + '.join(it['files'])}）：{e}", "error")
                continue
            self._store.update(it, yt_dirty=[])
            done += 1
            self._say(f"✓ 已更新：{it['title']}")
            yt_date = ((it.get("yt") or [{}])[0] or {}).get("recording_date")
            if yt_date and yt_date != it["recording_date"][:10]:
                self._say(f"  拍攝日期 {it['recording_date'][:10]} 要到 Studio 改（YouTube 上是 {yt_date}）")
        self._say(f"已更新 {done} / {len(todo)} 支")
        self._sync_youtube()  # read back what YouTube has now
        return self._replan()

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

    _FIELD_NAMES = {"title": "標題", "description": "說明", "local_time": "拍攝時間", "tz": "時區", "place": "地點名稱",
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
        """Being uploaded right now. (Uploaded ones can be edited and sent with push_to_youtube.)"""
        return item_id in self._uploading_ids

    _YT_FIELDS = {"title": "title", "local_time": "title", "tz": "title", "description": "description"}

    def _mark_for_youtube(self, item: dict, fields: dict, ov: dict):
        """An uploaded video was edited here: remember which YouTube fields to send later.

        Its title is seeded from YouTube's current one first, so a change of time only
        rewrites the leading date/time and never drops words typed in Studio.
        """
        rec = self._store.lookup(item) or {}
        dirty = set(rec.get("yt_dirty") or [])
        for k, v in fields.items():
            target = self._YT_FIELDS.get(k)
            if not target:
                continue
            if v is None and k in ("title", "description"):  # 還原 = back to what YouTube has
                dirty.discard(target)
                continue
            if target == "title" and "title" not in ov and k != "title":
                ov["title"] = item["title"]
            dirty.add(target)
        self._store.update(item, yt_dirty=sorted(dirty))

    def edit(self, ids: list[str], fields: dict):
        if any(k != "include" for k in fields):
            skipped = [i for i in ids if self._locked(i)]
            if skipped:
                self._say("略過正在上傳的錄影，等它傳完再改")
            ids = [i for i in ids if i not in skipped]
        by_id = {it["id"]: it for it in self._items}
        if ids and fields.get("place") and fields.get("lat") is not None and fields.get("lon") is not None:
            self._remember_place(fields["place"], fields["lat"], fields["lon"])
        for i in ids:
            if any(k != "include" for k in fields):  # ticks are too chatty to log
                self._log_edit(i, fields)
            ov = self._overrides.setdefault(i, {})
            it = by_id.get(i)
            if it and it["status"] in ("uploaded", "partial"):
                self._mark_for_youtube(it, fields, ov)
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

    def _scanned_photo(self, folder: str, name: str) -> str | None:
        """Only photos found by the last scan can be opened from the page."""
        if not any(p["folder"] == folder and p["name"] == name for p in self._photos):
            return None
        p = os.path.join(folder, name)
        return p if os.path.isfile(p) else None

    def reveal_photo(self, folder: str, name: str):
        p = self._scanned_photo(folder, name)
        if not p:
            return False
        subprocess.Popen(["explorer", f"/select,{os.path.normpath(p)}"])  # noqa: S603,S607
        return True

    def open_photo(self, folder: str, name: str):
        p = self._scanned_photo(folder, name)
        if not p:
            return False
        os.startfile(p)  # noqa: S606 - Windows only, user-initiated
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
        self._stop_after = False
        self._worker = threading.Thread(target=self._upload_all, daemon=True)
        self._worker.start()
        return True

    def cancel(self):
        self._cancel = True
        self._say("停止中…正在傳的會中斷，下次從中斷處接著傳")
        return True

    def close_question(self) -> str | None:
        """What to ask before the window closes, or None to just close."""
        if not (self._worker and self._worker.is_alive()):
            return None
        if self._quota_until and not self._uploading_ids:
            return (f"正在等 YouTube 配額重置（{self._quota_until:%H:%M} 自動繼續）。\n\n"
                    "關閉後就不會自動繼續，要再開程式按「開始上傳」。\n\n確定要關閉嗎？")
        return (f"正在上傳 {len(self._uploading_ids)} 支影片。\n\n"
                "關閉後會中斷，下次開始上傳時從中斷處接著傳（一週內有效；改了標題或說明就要從頭傳）。\n\n"
                "確定要關閉嗎？")

    def shut_down(self, timeout: float = CLOSE_WAIT) -> bool:
        """The window is closing: stop the uploads where they are (each saves its place before
        the next chunk) and wait for them. False if they did not stop in time."""
        self._closed = True
        if not (self._worker and self._worker.is_alive()):
            return True
        self._cancel = True
        log.info("視窗關閉：停止上傳，下次從中斷處接著傳")
        self._worker.join(timeout)
        return not self._worker.is_alive()

    def stop_after_current(self, on: bool = True):
        """Finish the videos being uploaded, start no new ones. Calling again with False undoes it."""
        self._stop_after = bool(on)
        self._say("正在傳的傳完就停止" if on else "已取消「傳完這些停止」，會繼續傳下去")
        return self._stop_after

    def set_parallel(self, n):
        self._settings.parallel_uploads = max(1, min(MAX_PARALLEL, int(n)))
        self._settings.save()
        return self._settings.parallel_uploads

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
            self._flush_playlist(yt)  # videos a crash or quota stop left out of their playlist
        except Exception as e:  # noqa: BLE001
            log.exception("setup failed")
            self._emit("done", msg=f"無法開始上傳：{e}", level="error")
            return

        width = max(1, min(MAX_PARALLEL, int(s.parallel_uploads or 1)))
        self._say(f"開始上傳 {len(todo)} 段，同時傳 {width} 支")
        self._sent, self._batch_bytes = {}, sum(i["size"] for i in todo) or 1
        self._taskbar.progress(0)
        order = {i["id"]: k for k, i in enumerate(todo)}
        waiting = list(todo)           # table order; a recording put back keeps its place
        running = {}                   # future -> item
        # order chain: each recording opens its YouTube session after the one before it, and
        # sends its last chunk only after the one before it has finished, so videos are
        # finished (and listed in Studio) in table order. One waiting for its last chunk is
        # "parked" and does not count against `width`.
        prev_started, prev_done = threading.Event(), threading.Event()
        prev_started.set()
        prev_done.set()
        self._parked.clear()
        ok, failed, hold, signed_out, halted = 0, False, False, None, False
        quota_until = None             # local time the daily quota resets, while waiting for it
        with ThreadPoolExecutor(width + MAX_PARKED) as pool:
            while waiting or running:
                while (waiting and len(running) - len(self._parked) < width
                       and len(running) < width + MAX_PARKED and not hold
                       and not self._cancel and not self._stop_after):
                    queued = waiting.pop(0)
                    # edits made while earlier videos were uploading re-plan self._items;
                    # take this recording's latest version, not the snapshot from the start
                    item = next((i for i in self._items if i["id"] == queued["id"]), queued)
                    self._uploading_ids.add(item["id"])
                    n = len(todo) - len(waiting)
                    self._say(f"[{n}/{len(todo)}] {item['title']}  ({' + '.join(item['files'])})")
                    started, done = threading.Event(), threading.Event()
                    # its own connection: the window may use self._yt at the same time
                    # (更新到 YouTube works during uploads) and httplib2 is not thread-safe
                    svc = youtube.service_copy(yt)
                    fut = pool.submit(self._upload_item, svc, item, (prev_started, started, prev_done, done))
                    running[fut] = item
                    prev_started, prev_done = started, done
                if not running:
                    if quota_until and waiting and self._wait_for_quota(quota_until):
                        quota_until, hold = None, False
                        self._say("配額已重置，繼續上傳")
                        continue
                    break
                # wake up now and then too: a recording that parks frees its slot for the next
                finished, _ = wait(running, timeout=1, return_when=FIRST_COMPLETED)
                for fut in sorted(finished, key=lambda f: order.get(running[f]["id"], 0)):
                    item = running.pop(fut)
                    self._uploading_ids.discard(item["id"])
                    try:
                        fut.result()
                        ok += 1
                    except youtube.SignedOut as e:
                        # expired mid-batch: let the others end, sign in once, then redo these
                        # (an upload already under way goes on from where it stopped)
                        signed_out, hold = e, True
                        self._set_status(item, "pending")
                        waiting.append(item)
                        waiting.sort(key=lambda i: order.get(i["id"], 0))
                    except youtube.QuotaExceeded as e:
                        # not its fault: let the ones under way finish, then wait for the reset
                        # and go on by ourselves, this one first
                        self._set_status(item, "pending")
                        waiting.append(item)
                        waiting.sort(key=lambda i: order.get(i["id"], 0))
                        log.info("配額用完：%s（%s）", e, " + ".join(item["files"]))
                        if not quota_until:
                            quota_until = quota_reset()
                            self._say(f"YouTube 今日配額用完，正在傳的會傳完；"
                                      f"{quota_until:%H:%M} 配額重置後自動繼續（程式開著就好）", "error")
                        hold = True
                    except youtube.Cancelled:
                        self._set_status(item, "pending")
                    except Exception as e:  # noqa: BLE001
                        log.exception("upload failed")
                        self._set_status(item, "error", error=str(e))
                        self._say(f"失敗：{item['title']}：{e}", "error")
                        self._taskbar.state(taskbar.ERROR)
                        if not halted:
                            # pause everything so this one can be redone before the later ones
                            # finish (Studio lists by upload date); the others resume next time
                            self._say("已暫停整批，保持影片順序。正在傳的會在下次開始上傳時從中斷處接著傳。", "error")
                        failed, halted, hold = True, True, True
                        self._cancel = True
                        waiting.clear()
                try:  # in file order, as far as the earlier ones are done
                    self._flush_playlist(yt, held=waiting if signed_out else ())
                except youtube.QuotaExceeded:
                    if not quota_until:
                        quota_until = quota_reset()
                        self._say(f"配額用完，有些影片還沒加入播放清單；{quota_until:%H:%M} 配額重置後自動補上並繼續", "error")
                    hold = True
                except Exception as e:  # noqa: BLE001
                    log.warning("could not add to the playlist yet: %s", e)
                if signed_out and not running:
                    self._signed_out(signed_out)
                    self._say("請在跳出的瀏覽器頁面重新登入，登入後會從中斷處接著上傳")
                    try:
                        yt = self._signed_in()
                    except Exception as e:  # noqa: BLE001
                        log.exception("sign-in failed")
                        self._say(f"無法重新登入：{e}", "error")
                        failed = True
                        break
                    signed_out, hold = None, False
        self._uploading_ids.clear()
        # done: clear the bar (or leave it red if something failed) and blink the button
        self._taskbar.state(taskbar.ERROR if failed else taskbar.NONE)
        self._taskbar.flash()
        stopped = "（有一支失敗，已暫停）" if halted else "（配額用完，等重置時停止）" if quota_until else "（已停止）" if self._cancel else "（傳完那些後停止）" if self._stop_after and ok < len(todo) else ""
        self._emit("done", msg=f"完成 {ok} / {len(todo)} 段{stopped}")

    def _wait_for_quota(self, until: datetime) -> bool:
        """Sleep until the quota resets; False if the user stopped meanwhile."""
        self._emit("quota_wait", until=until.timestamp())
        self._quota_until = until
        try:
            while datetime.now() < until:
                if self._cancel or self._stop_after:
                    return False
                time.sleep(QUOTA_POLL)
            return True
        finally:
            self._quota_until = None
            self._emit("quota_wait", until=None)

    def _flush_playlist(self, yt, held=()):
        """Add finished uploads to their playlist in file-name order.

        Uploads finish out of order when several run at once, so each one waits in state.json
        ("playlist_add") until no recording before it is still uploading (or held for a retry).
        A crash or a quota stop leaves them there; the next batch adds them first.
        """
        ready = sorted(((rec.get("files") or [""])[0].upper(), fp, dict(rec["playlist_add"]))
                       for fp, rec in list(self._store.data.items()) if rec.get("playlist_add"))
        if not ready:
            return
        busy = [i["files"][0].upper() for i in self._items if i["id"] in self._uploading_ids]
        busy += [i["files"][0].upper() for i in held]
        first_busy = min(busy) if busy else None
        for first, fp, add in ready:
            if first_busy and first > first_busy:
                break  # an earlier recording is still uploading: keep the order
            ids = list(add["ids"])
            while ids:
                youtube.add_to_playlist(yt, add["playlist"], ids[0])
                ids.pop(0)
                self._store.patch(fp, playlist_add={**add, "ids": ids} if ids else None)

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
            try:
                return [(merge.merge(paths, cancel=lambda: self._cancel), title, True)]
            except RuntimeError:
                if self._cancel:  # stopped while merging: not a failure, just not done yet
                    raise youtube.Cancelled() from None
                raise
        return [(p, f"{title} ({i}/{len(paths)})", False) for i, p in enumerate(paths, 1)]

    def _taskbar_progress(self, item_id, sent):
        self._sent[item_id] = sent
        self._taskbar.progress(sum(self._sent.values()) / self._batch_bytes)

    def _upload_item(self, yt, item, chain=None):
        """Upload one recording (all its parts). Runs in a pool thread.

        chain = (prev_started, started, prev_done, done) keeps the table order (see _upload_all).
        """
        prev_started, started, prev_done, done = chain or (None, None, None, None)
        try:
            self._upload_parts(yt, item, prev_started, started, prev_done)
        finally:
            self._parked.discard(item["id"])
            for ev in (started, done):
                if ev:
                    ev.set()

    def _upload_parts(self, yt, item, prev_started, started, prev_done):
        s = self._settings
        prev = self._store.lookup(item) or {}
        done_ids = prev.get("video_ids", []) if prev.get("status") == "partial" else []
        item["video_ids"] = list(done_ids)
        sources = self._sources(item)
        while prev_started and not prev_started.wait(1):
            if self._cancel:
                raise youtube.Cancelled()
        for idx, (path, title, temp) in enumerate(sources):
            if idx < len(done_ids):
                continue
            self._emit("item", id=item["id"], status="uploading")
            body = self._upload_body(item, title)
            size = os.path.getsize(path)
            # an upload stopped earlier (stop button, crash, power cut) goes on from where it was,
            # if it is the same file and the same title / description / date
            sig = _resume_sig(path, size, idx, body)
            r = (self._store.lookup(item) or {}).get("resume") or {}
            resume_uri = r.get("uri") if r.get("sig") == sig else None
            sess = dict(r) if resume_uri else {}  # saved with how far it got, for 已中斷 N%
            if resume_uri:
                was = (self._resume_info(item) or {}).get("pct")
                self._say(f"從中斷處接著傳{f'（上次到 {was:.0f}%）' if was is not None else ''}：{title}")
                if started:  # its session is already open (on_session is not called again)
                    started.set()
            elif r.get("uri"):
                self._say(f"標題或說明改過，從頭傳：{title}")

            def on_session(uri, _sig=sig, _idx=idx):
                sess.clear()
                sess.update(uri=uri, sig=_sig, at=datetime.now().isoformat(timespec="seconds"),
                            part=_idx + 1, parts=len(sources))
                self._store.update(item, resume=dict(sess))
                if started:
                    started.set()

            t0, first, last, waited = time.time(), None, (0, size), 0.0

            def progress(done, total, _idx=idx):
                nonlocal first, last
                if first is None:
                    first = (time.time(), done)
                last = (done, total)
                if sess.get("uri") and total and done < total:
                    sess.update(done=done, total=total, part=_idx + 1, parts=len(sources))
                    self._store.update(item, resume=dict(sess))
                self._emit("progress", id=item["id"], part=_idx + 1, parts=len(sources),
                           done=done, total=total)
                self._taskbar_progress(item["id"], item["size"] * (_idx + (done / total if total else 0)) / len(sources))

            def before_last(_idx=idx):
                nonlocal waited
                if not prev_done or prev_done.is_set():
                    return
                w0 = time.time()
                self._parked.add(item["id"])
                self._emit("progress", id=item["id"], part=_idx + 1, parts=len(sources),
                           done=last[0], total=last[1], waiting=True)
                while not prev_done.wait(1) and not self._cancel:
                    pass
                self._parked.discard(item["id"])
                waited = time.time() - w0

            vid = youtube.upload(yt, path, body, progress=progress, cancel=lambda: self._cancel,
                                 resume_uri=resume_uri, on_session=on_session,
                                 before_last=before_last)
            item["video_ids"].append(vid)
            self._store.append_history(item, vid, title, s.playlist)
            # the playlist add waits for the recordings before this one (see _flush_playlist)
            old = (self._store.lookup(item) or {}).get("playlist_add") or {}
            ids = list(old.get("ids", [])) + ([vid] if s.playlist_id else [])
            extra = {"playlist_add": {"playlist": old.get("playlist") or s.playlist_id, "ids": ids}} if ids else {}
            self._set_status(item, "partial" if idx + 1 < len(sources) else "uploaded", resume=None, **extra)
            self._say(f"✓ https://youtu.be/{vid}  {_speed_note(size, t0, first, waited)}")
            if temp and s.delete_merged:
                try:
                    os.remove(path)
                except OSError as e:
                    log.warning("could not delete merged file: %s", e)


def _speed_note(size: int, t0: float, first, waited: float = 0.0) -> str:
    """'2.1 GB · 3 分 12 秒 · 平均 3.5 MB/s' for the log, so slow-downs show up afterwards.

    Measured from the first progress report when there is one, so bytes a resumed upload
    did not have to send again are not counted.
    """
    now = time.time() - waited  # time spent waiting for the previous video is not upload time
    secs = now - t0
    rate_t, rate_b = (first[0], size - first[1]) if first and now - first[0] > 1 else (t0, size)
    speed = rate_b / max(now - rate_t, 0.001)
    took = f"{int(secs // 60)} 分 {int(secs % 60)} 秒" if secs >= 60 else f"{secs:.0f} 秒"
    return f"{size / 1e9:.2f} GB · {took} · 平均 {speed / 1e6:.1f} MB/s"


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
    window = webview.create_window(WINDOW_TITLE, _resource("ui", "index.html"),
                                   js_api=api, width=geo["width"], height=geo["height"],
                                   x=geo["x"], y=geo["y"], maximized=geo["maximized"],
                                   min_size=(1000, 650))
    api._window = window
    _remember_window(window, api._settings)

    def closing():  # runs on the GUI thread; False keeps the window open
        question = api.close_question()
        return question is None or _ask_yes_no(window, question)

    window.events.closing += closing
    webview.start(http_server=True, debug="--debug" in sys.argv)
    # the upload threads would otherwise keep the process alive, uploading with no window
    if not api.shut_down():
        log.warning("uploads did not stop in %ss; exiting anyway (they resume next time)", CLOSE_WAIT)
    logging.shutdown()
    os._exit(0)


def _ask_yes_no(window, text: str) -> bool:
    """A Windows message box owned by the window (No is the default button)."""
    try:
        hwnd = window.native.Handle.ToInt32()
    except Exception:  # noqa: BLE001 - no handle: a task-modal box works too
        hwnd = 0
    flags = 0x4 | 0x30 | 0x100 | (0 if hwnd else 0x2000)  # YESNO | WARNING | DEFBUTTON2 | TASKMODAL
    return ctypes.windll.user32.MessageBoxW(hwnd, text, WINDOW_TITLE, flags) == 6  # IDYES
