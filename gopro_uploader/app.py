"""Desktop window (pywebview) and the Python API exposed to the HTML UI."""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import webview

from . import config, geocode, merge, preview, taskbar, youtube
from .config import TITLE_FORMATS, Settings
from .planner import build_plan, detect_camera_offset, retime_title, title_time_fix, zone_hints
from .scanner import list_photos, scan
from .store import Edits, Store, fingerprint, setup_logging
from .store import export_logs as store_export_logs

log = logging.getLogger("app")

WINDOW_TITLE = "GoPro → YouTube 上傳工具"
MAX_PARALLEL = 8  # HDD reads and YouTube's per-account limits are unknown beyond this
# uploads that may wait for their last chunk on top of settings.parallel_uploads; a parked one
# only holds an idle connection, so keep this high: a long video at the front must not leave
# the short ones behind it filling every slot (seen with 6: only 2 of 6 kept sending)
MAX_PARKED = 40
QUOTA_POLL = 10  # seconds between checks while waiting for the quota to reset
RESUME_FOR = timedelta(days=7)  # YouTube keeps a stopped upload session about a week
CLOSE_WAIT = 20  # seconds to let uploads stop and save their place when the window closes
# while waiting for the quota, try again this often: the reset time is Google's word for the API
# quota, not something seen for the channel's upload limit (it could be a rolling window)
QUOTA_PROBE = timedelta(hours=1)
CHANNEL_FILE = "channel.json"  # the channel's videos as last read, to mark what is already there
SAME_SIZE_MIN = 1_000_000  # smaller files can share a size by chance


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


_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d):\d\d,\d+ \w+ app: (.*)$")


def by_log(logs_dir: str, by_vid: dict, by_files: dict, by_title: dict | None = None) -> dict[str, dict]:
    """From the run logs: which recordings this app fixed (修正) or sent a title to (更新到 YouTube).
    by_vid: video id -> fingerprint; by_files: "GX01.MP4 + GX02.MP4" -> fingerprint;
    by_title: title on YouTube now -> fingerprint, for a title changed through its time."""
    out: dict[str, dict] = {}
    names = sorted(f for f in os.listdir(logs_dir) if f.endswith(".log")) if os.path.isdir(logs_dir) else []
    for name in names:
        fixing, titled = False, {}  # title typed here -> files, for the "✓ 已更新：title" lines
        try:
            with open(os.path.join(logs_dir, name), encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            m = _LOG_LINE.match(line.rstrip())
            if not m:
                continue
            when, msg = m.group(1).replace(" ", "T"), m.group(2)
            if msg.startswith("修正 ") and msg.endswith("支手動上傳的影片…"):
                fixing = True
            elif msg.startswith("修正完成") or msg.startswith("已停止"):
                fixing = False
            elif fixing and msg.startswith("✓ ") and "https://youtu.be/" in msg:
                fp = by_vid.get(msg.rsplit("https://youtu.be/", 1)[1].split()[0])
                if fp:
                    out.setdefault(fp, {}).setdefault("fixed", when)
            elif msg.startswith("修改 ") and "：標題 → " in msg:
                files, title = msg[3:].split("：標題 → ", 1)
                titled[title.strip()] = files.strip()
            elif msg.startswith("✓ 已更新："):
                title = msg[len("✓ 已更新："):].strip()
                fp = by_files.get(titled.get(title, "")) or (by_title or {}).get(title)
                if fp:
                    out.setdefault(fp, {})["pushed"] = when
    return out


class Api:
    def __init__(self, log_file: str):
        self._window = None
        # a browser sign-in waiting: the window shows 重新開啟登入頁 / 取消
        youtube.on_sign_in_wait = lambda waiting: self._emit("signin_wait", waiting=waiting)
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
        self._cut: set[str] = set()  # recordings behind a failed one: stop these, not the others
        self._worker: threading.Thread | None = None
        self._preview: preview.PreviewServer | None = None
        self._uploading_ids: set[str] = set()  # several upload at once (settings.parallel_uploads)
        self._parked: set[str] = set()  # sent all but the last chunk, waiting for the one before
        self._taskbar = taskbar.Taskbar(WINDOW_TITLE)
        self._sent: dict[str, float] = {}  # taskbar: recording id -> bytes sent; plus the batch total
        self._batch_bytes = 1
        self._told_signed_out = False
        self._quota_until: datetime | None = None  # while the batch waits for the quota reset
        # a recording was refused for the quota: the ones behind it don't open a session, or one
        # let in a moment later would land in Studio before it
        self._quota_hit = threading.Event()
        self._closed = False  # window gone: nothing to send events to
        self._compared = self._load_channel()
        self._channel = self._index_channel(self._compared.get("videos", []))  # file size -> videos
        self._compare_stop = False
        self._fix_stop = False

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
                                 place_lookup=self._place_lookup,
                                 zone_hints=zone_hints(self._recs, self._yt_titles(), self._settings))
        placeholders = self._placeholders()
        for it in self._items:  # "uploading" lives only in memory, not in state.json
            if it["id"] in self._uploading_ids and it["status"] != "uploaded":
                it["status"] = "uploading"
            self._attach_youtube(it)
            self._check_title_time(it)
            self._attach_channel(it, placeholders)
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
        rec = self._store.lookup(it) or {}
        it["hand"] = bool(rec.get("hand"))  # uploaded by hand, found by 比對
        it["by_app"] = rec.get("by_app") or {}  # what this app did to a hand upload: {"fixed": when, "pushed": when}
        if it["status"] not in ("uploaded", "partial") or not it.get("video_ids"):
            return
        vids = it["video_ids"]
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
        # an edit made before it went up (or before 修正) is on YouTube already: not 已修改 any more
        it["edited"] = [k for k in it.get("edited", []) if k not in ("title", "description") or k in pending]
        it["yt_place"] = live[0].get("place") or ""
        it["yt_has_location"] = live[0].get("lat") is not None

    def _yt_titles(self) -> dict[str, str]:
        """recording id -> its title on YouTube (as last read), for zone_hints."""
        out = {}
        for key, fp in self._fps.items():
            rec = self._store.data.get(fp) or {}
            saved = rec.get("yt") or {}
            live = [saved[v] for v in rec.get("video_ids") or [] if saved.get(v)]
            if rec.get("status") in ("uploaded", "partial") and live:
                out[key] = live[0].get("title") or ""
        return out

    @staticmethod
    def _check_title_time(it):
        """An uploaded video whose title starts with another date / time than its file says:
        typed by hand, copied from the row above, or the camera's home clock with " JST" added."""
        it["title_should"] = None
        if it["status"] in ("uploaded", "partial") and it["time_known"] and "title" not in (it.get("yt_pending") or []):
            it["title_should"] = title_time_fix(it["title"], datetime.fromisoformat(it["local"]))

    # ------------------------------------------------- already on the channel?
    @staticmethod
    def _index_channel(videos: list[dict]) -> dict[int, list[dict]]:
        index: dict[int, list[dict]] = {}
        for v in videos:
            if (v.get("size") or 0) >= SAME_SIZE_MIN:
                index.setdefault(v["size"], []).append(v)
        return index

    def _load_channel(self) -> dict:
        """The last comparison the user asked for (nothing is read from YouTube without asking)."""
        try:
            with open(config.path(CHANNEL_FILE), encoding="utf-8") as f:
                data = json.load(f)
            data["videos"]
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _forget_channel(self):
        """Another account may sign in next: its channel is not this one."""
        self._compared, self._channel = {}, {}
        try:
            os.remove(config.path(CHANNEL_FILE))
        except OSError:
            pass

    def _attach_channel(self, it, placeholders=frozenset()):
        """A file with the exact size of a video the last comparison read was uploaded before,
        even under another name or by hand. A merge changes the size: compare_result lists those."""
        it["on_channel"], it["on_channel_some"], it["on_channel_draft"] = [], False, False
        if it["status"] not in ("pending", "error", "missing", "partial"):
            return
        own = set(it["video_ids"])
        hits = [[v for v in self._channel.get(size, []) if v["id"] not in own] for size in it.get("sizes", [])]
        if any(hits):
            it["on_channel"] = list({v["id"]: v for found in hits for v in found}.values())
            it["on_channel_some"] = not all(hits)
            it["on_channel_draft"] = any(self._is_draft(v, placeholders, it.get("files", [])) for v in it["on_channel"])

    def _placeholders(self) -> set[str]:
        """Titles of hand uploads nobody filled in (YouTube's default): one shared by 3+ private
        hand uploads. Kept with the comparison, so the last few drafts still count once the
        others have been fixed."""
        videos = self._compared.get("videos", [])
        count = Counter(v.get("title") for v in videos if v.get("name") and v.get("privacy") == "private")
        return set(self._compared.get("placeholders") or []) | {t for t, n in count.items() if t and n >= 3}

    def _remember_placeholders(self) -> set[str]:
        ph = self._placeholders()
        if self._compared and ph != set(self._compared.get("placeholders") or []):
            self._compared["placeholders"] = sorted(ph)
            self._save_compared()
        return ph

    @staticmethod
    def _is_draft(v, placeholders, files) -> bool:
        """Uploaded by hand with nothing filled in: a placeholder title, or just the file name."""
        title = (v.get("title") or "").strip()
        names = {f.upper() for f in files} | {os.path.splitext(f)[0].upper() for f in files}
        return title in placeholders or title.upper() in names

    def _adopt_hand(self) -> int:
        """Hand uploads with this folder's file names and exact sizes are its uploads too: record
        them in state.json like this app's own, so they show what YouTube has, can be edited and
        sent with 更新到 YouTube, and 核對 checks them. Drafts wait until 修正 has filled them in."""
        ph = self._remember_placeholders()
        found: dict[str, tuple[dict, dict]] = {}
        for v, it, n in self._hand_matches():
            found.setdefault(it["id"], (it, {}))[1].setdefault(n, v)
        new = 0
        for it, got in found.values():
            if (it["status"] != "pending" or len(got) != len(it["files"])
                    or any(self._is_draft(v, ph, it["files"]) for v in got.values())):
                continue
            ids = [got[n]["id"] for n in range(len(got))]
            self._store.update(it, status="uploaded", video_ids=ids, hand=True)
            log.info("hand upload recorded as uploaded: %s -> %s", " + ".join(it["files"]), ", ".join(ids))
            new += 1
        if new:
            self._say(f"{new} 段手動上傳的影片（檔名和大小都相同）記為已上傳")
        return new

    def _note_by_app(self, it, what, when=None):
        """Remember that this app changed a hand upload, to tell it from what was typed in Studio."""
        rec = self._store.lookup(it) or {}
        by = dict(rec.get("by_app") or {})
        by[what] = when or datetime.now().isoformat(timespec="minutes")
        self._store.update(it, by_app=by)

    def _backfill_by_app(self):
        """Fixes and title updates made before by_app was recorded, from the run logs (once)."""
        if self._settings.by_app_backfilled:
            return
        by_vid = {v: fp for fp, rec in self._store.data.items() if rec.get("hand") for v in rec.get("video_ids") or []}
        by_files = {" + ".join(rec.get("files") or []): fp for fp, rec in self._store.data.items() if rec.get("hand")}
        by_title = {(rec.get("yt") or {}).get(v, {}).get("title"): fp for fp, rec in self._store.data.items()
                    if rec.get("hand") for v in (rec.get("video_ids") or [])[:1] if (rec.get("yt") or {}).get(v)}
        found = by_log(config.path("logs"), by_vid, by_files, by_title)
        for fp, what in found.items():
            rec = self._store.data.get(fp) or {}
            by = dict(rec.get("by_app") or {})
            for k, when in what.items():
                by.setdefault(k, when)
            self._store.patch(fp, by_app=by)
        if found:
            log.info("by_app filled in from the logs for %d recordings", len(found))
        self._settings.by_app_backfilled = True
        self._settings.save()

    def compare_options(self):
        """What the 比對 dialog offers. Reads only the playlist names (1 quota unit per 50)."""
        known = [it["local"][:10] for it in self._items if it["time_known"] and not it["time_suspect"]]
        out = {"earliest": min(known) if known else "", "playlists": [],
               "signed_in": bool(self._yt), "target": self._settings.playlist_id or "",
               "last": {k: self._compared.get(k) for k in ("read_at", "scope", "complete")} if self._compared else None}
        if self._yt:
            try:
                out["playlists"] = [{"id": p["id"], "title": p["title"], "count": p["count"]}
                                    for p in youtube.list_playlists(youtube.service_copy(self._yt))]
            except youtube.SignedOut as e:
                self._signed_out(e)
                out["signed_in"] = False
            except Exception as e:  # noqa: BLE001 - offline: the dialog works without them
                log.warning("could not read the compare options: %s", e)
        return out

    def cancel_compare(self):
        self._compare_stop = True
        return True

    def compare(self, scope: str, since: str = "", playlist_id: str = ""):
        """Read the range the user picked and compare it with this folder. Only reads."""
        self._compare_stop = False
        self._with_sign_in(lambda yt: self._read_for_compare(youtube.service_copy(yt), scope, since, playlist_id))
        self._replan()
        n = sum(bool(it["on_channel"]) for it in self._items)
        note = "" if self._compared["complete"] else "（中途取消，結果不完整）"
        self._say(f"比對完成{note}：讀了 {len(self._compared['videos'])} 支影片，這個資料夾有 {n} 段已經傳過")
        if self._adopt_hand():
            self._replan()
            self._sync_youtube()  # their titles, for the zone they were shot in and the time check
        return self.compare_result()

    def _read_for_compare(self, yt, scope, since, playlist_id):
        s = self._settings
        stop = lambda: self._compare_stop
        page = lambda n: self._emit("compare", read=n)
        self._say("正在讀取 YouTube 上的影片…")
        if scope == "playlist":
            videos, done = youtube.read_playlist(yt, playlist_id, on_page=page, cancel=stop)
            text = "這個播放清單"
        else:
            uploads = youtube.uploads_playlist(yt)
            if not uploads:
                raise ValueError("這個帳號沒有 YouTube 頻道")
            # a day of margin: the folder's dates are local and YouTube's are UTC
            start = ((datetime.fromisoformat(since) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
                     if scope == "since" else "")
            videos, done = youtube.read_playlist(yt, uploads, since=start, on_page=page, cancel=stop)
            text = f"{since} 以後上傳的影片" if scope == "since" else "整個頻道"
        # the target playlist is always read too: its order is what the videos are compared with
        order = youtube.playlist_videos(yt, s.playlist_id) if s.playlist_id and not stop() else []
        have = {v["id"] for v in videos}
        extra = youtube.file_info(yt, [v for v in order if v not in have]) if order else {}
        videos += [extra[v] for v in order if v in extra]
        where = {v: k for k, v in enumerate(order)}
        for k, v in enumerate(videos):
            v["pos"], v["read"] = where.get(v["id"]), k
        self._compared = {"read_at": datetime.now().isoformat(timespec="minutes"), "scope": text, "complete": done,
                          "target": s.playlist if order else "", "videos": videos}
        self._channel = self._index_channel(videos)
        self._save_compared()

    def compare_result(self):
        """A row per local recording, then a row per video that matched none of them, so anything
        the size missed (a merge, an export) can be spotted by eye."""
        self._replan()
        videos = self._compared.get("videos", [])
        by_id = {v["id"]: v for v in videos}
        by_name: dict[str, list] = {}
        for v in videos:
            if v.get("name"):
                by_name.setdefault(v["name"].upper(), []).append(v)
        used, rows = set(), []
        for it in self._items:
            mine = [by_id.get(i, {"id": i}) for i in it["video_ids"]] if it["status"] in ("uploaded", "partial") else []
            named = [v for f in it["files"] for v in by_name.get(f.upper(), [])]
            if it["status"] == "uploaded":
                kind, found = "uploaded", mine
            elif it["on_channel"]:
                kind, found = "some" if it["on_channel_some"] else "uploaded", mine + it["on_channel"]
            elif mine:
                kind, found = "some", mine
            elif named:
                kind, found = "maybe", named
            else:
                kind, found = "none", []
            used |= {v["id"] for v in found}
            rows.append({"kind": kind, "video": found[0] if found else None, "more": max(0, len(found) - 1),
                         "local": {"id": it["id"], "title": it["title"], "files": it["files"], "size": it["size"],
                                   "when": it["local"][:16]}})
        # uploads this app made from other folders aren't candidates for a missed match
        ours = {i for rec in self._store.data.values() for i in rec.get("video_ids") or []}
        rows += [{"kind": "elsewhere" if v["id"] in ours else "only", "video": v, "more": 0, "local": None}
                 for v in videos if v["id"] not in used]
        return {"rows": rows, "plan": self._replan(), "scope": self._compared.get("scope", ""),
                "read_at": self._compared.get("read_at", ""), "complete": self._compared.get("complete", True),
                "target": self._compared.get("target", ""), "read": len(videos)}

    def clear_compare(self):
        self._forget_channel()
        return self._replan()

    # ------------------------------------------ fix videos uploaded by hand
    def _hand_matches(self):
        """(video, item, chapter index) for every video the last comparison read that this app
        didn't upload and whose file name and exact size are a chapter of a scanned recording."""
        self._replan()
        ours = {i for rec in self._store.data.values() for i in rec.get("video_ids") or []}
        by_file = {}
        for it in self._items:
            for n, (name, size) in enumerate(zip(it["files"], it.get("sizes") or [])):
                by_file[(name.upper(), size)] = (it, n)
        out = []
        for v in self._compared.get("videos", []):
            hit = by_file.get(((v.get("name") or "").upper(), v.get("size")))
            if hit and v["id"] not in ours:
                out.append((v, *hit))
        order = {it["id"]: k for k, it in enumerate(self._items)}
        return sorted(out, key=lambda m: (order[m[1]["id"]], m[2]))

    @staticmethod
    def _hand_title(it, n):
        return it["title"] + (f" ({n + 1}/{len(it['files'])})" if len(it["files"]) > 1 else "")

    def hand_fixes(self):
        """What 修正手動上傳的影片 would do. Drafts (nothing filled in) are ticked by default; whole
        recordings uploaded by hand and filled in are already recorded as uploaded, not listed."""
        matches = self._hand_matches()
        ph = self._placeholders()
        s = self._settings
        return {"rows": [{"id": v["id"], "item": it["id"], "title": v["title"], "privacy": v.get("privacy"),
                          "new_title": self._hand_title(it, n), "file": it["files"][n],
                          "in_list": v.get("pos") is not None, "tick": self._is_draft(v, ph, it["files"])}
                         for v, it, n in matches],
                "privacy": s.privacy, "playlist": "" if s.no_playlist else s.playlist,
                "playlist_id": "" if s.no_playlist else s.playlist_id}

    def cancel_fix(self):
        self._fix_stop = True
        return True

    def fix_hand_uploads(self, video_ids: list[str]):
        """Fill in this app's settings on the picked hand uploads, in file order, and add them to
        the chosen playlist at their place by file order. Stops at the daily quota (about 100
        units each); what is done stays done, so running it again goes on."""
        self._fix_stop = False
        self._remember_placeholders()  # before the titles change, so the drafts left still count
        want = set(video_ids)
        todo = [m for m in self._hand_matches() if m[0]["id"] in want]
        if not todo:
            return self.hand_fixes()
        fixed = self._with_sign_in(lambda yt: self._fix_hand(youtube.service_copy(yt), todo)) or set()
        if self._adopt_hand():  # the fixed ones are no drafts any more
            self._replan()
            # the title edited here is on YouTube now: it isn't a pending change any more
            for it in self._items:
                if it["id"] in fixed and it["status"] == "uploaded":
                    self._note_by_app(it, "fixed")
                    self._overrides.get(it["id"], {}).pop("include", None)  # its tick meant "fix it": done
                    if self._overrides.get(it["id"], {}).pop("title", None):
                        self._save_edits(it["id"])
            self._replan()
            self._sync_youtube()
        return self.hand_fixes()

    def _fix_hand(self, yt, todo):
        s = self._settings
        pid = "" if s.no_playlist else s.playlist_id
        rank = self._playlist_rank()
        listed = youtube.playlist_videos(yt, pid) if pid else []
        self._say(f"修正 {len(todo)} 支手動上傳的影片…")
        done, fixed = 0, set()
        for k, (v, it, n) in enumerate(todo, 1):
            if self._fix_stop:
                self._say("已停止，剩下的下次再修正")
                break
            title = self._hand_title(it, n)
            try:
                youtube.apply_settings(yt, v["id"], self._upload_body(it, title))
                v["title"], v["privacy"] = title, s.privacy
                if pid and v["id"] not in listed:
                    self._add_in_place(yt, pid, v["id"], rank, listed)
            except youtube.QuotaExceeded:
                self._say("YouTube 今日配額用完，剩下的明天再按一次「修正」", "error")
                break
            except Exception as e:  # noqa: BLE001 - one bad video doesn't stop the rest
                log.exception("fixing %s failed", v["id"])
                self._say(f"修正失敗（{it['files'][n]}）：{e}", "error")
                continue
            finally:
                self._save_compared(listed if pid else None)
            done += 1
            fixed.add(it["id"])
            self._emit("fix", done=k, total=len(todo))
            self._say(f"✓ {title}  https://youtu.be/{v['id']}")
        self._say(f"修正完成 {done} / {len(todo)} 支")
        return fixed

    def _playlist_rank(self):
        """Where each video of this folder belongs in a playlist: (table row, chapter).
        Videos not in it have no rank and stay where they are."""
        row = {it["id"]: k for k, it in enumerate(self._items)}
        # state.json too: during an upload the row in _items may be an older copy than the worker's
        rank = {vid: (row[it["id"]], n) for it in self._items
                for n, vid in enumerate(it.get("video_ids") or (self._store.lookup(it) or {}).get("video_ids") or [])}
        rank.update({v["id"]: (row[it["id"]], n) for v, it, n in self._hand_matches()})
        return rank

    @staticmethod
    def _add_in_place(yt, pid, vid, rank, listed):
        """Insert vid before the first ranked video that comes after it (else append); keeps listed in step."""
        me = rank.get(vid)
        pos = len(listed) if me is None else next(
            (i for i, x in enumerate(listed) if x in rank and rank[x] > me), len(listed))
        youtube.add_to_playlist(yt, pid, vid, None if pos == len(listed) else pos)
        listed.insert(pos, vid)

    @staticmethod
    def _sorted_order(ids, rank):
        """The playlist order with this folder's videos sorted among the places they take up now."""
        mine = sorted((x for x in ids if x in rank), key=rank.__getitem__)
        it = iter(mine)
        return [next(it) if x in rank else x for x in ids]

    def sort_playlist(self):
        """Move this folder's videos in the picked playlist into table order (about 50 units a move)."""
        s = self._settings
        if s.no_playlist or not s.playlist_id:
            return {"ok": False, "error": "沒有選播放清單"}
        rank = self._playlist_rank()

        def run(yt):
            entries = youtube.playlist_entries(yt, s.playlist_id)
            want = self._sorted_order([v for _, v in entries], rank)
            moved = 0
            for i, vid in enumerate(want):
                if entries[i][1] == vid:
                    continue
                k = next(j for j in range(i, len(entries)) if entries[j][1] == vid)
                youtube.move_in_playlist(yt, s.playlist_id, entries[k][0], vid, i)
                entries.insert(i, entries.pop(k))
                moved += 1
            return moved

        try:
            moved = self._with_sign_in(lambda yt: run(youtube.service_copy(yt)))  # uploads may be running
        except youtube.QuotaExceeded:
            self._say("YouTube 今日配額用完，明天再排一次", "error")
            return {"ok": False, "error": "配額用完"}
        except Exception as e:  # noqa: BLE001
            log.exception("sorting the playlist failed")
            self._say(f"排播放清單失敗：{e}", "error")
            return {"ok": False, "error": str(e)}
        self._say(f"播放清單「{s.playlist}」已排好順序（移動 {moved or 0} 支）")
        return {"ok": True, "moved": moved or 0}

    def _save_compared(self, listed=None):
        """Keep the last comparison in step with what was just changed on YouTube."""
        if listed is not None:
            where = {vid: i for i, vid in enumerate(listed)}
            for v in self._compared.get("videos", []):
                v["pos"] = where.get(v["id"])
        with open(config.path(CHANNEL_FILE), "w", encoding="utf-8") as f:
            json.dump(self._compared, f, ensure_ascii=False)

    # ---------------------------------------------------------- 核對
    def verify(self):
        """Is what this folder uploaded really there, whole, and in order? Only reads."""
        self._replan()
        uploaded = [it for it in self._items if it["status"] in ("uploaded", "partial") and it.get("video_ids")]
        checks = [self._verify_counts()]
        if uploaded:
            checks += self._with_sign_in(lambda yt: self._verify_uploaded(youtube.service_copy(yt), uploaded))
            self._sync_youtube()
        bad = sum(c["level"] == "bad" for c in checks)
        if not uploaded:
            self._say("核對完成：這個資料夾沒有已上傳的影片，沒有可以核對的（頻道上有沒有，請用「和 YouTube 比對…」）")
        else:
            self._say(f"核對完成：{bad} 項有問題" if bad else "核對完成：沒有發現問題")
        return {"checks": checks, "plan": self._replan()}

    def _verify_uploaded(self, yt, uploaded):
        got = youtube.file_info(yt, [v for it in uploaded for v in it["video_ids"]])
        # hand uploads went up in whatever order they were dragged in; that can't be changed
        mine = [it for it in uploaded if not it.get("hand")]
        return [*self._verify_videos(uploaded, got), *self._verify_playlist(yt, uploaded),
                *([self._verify_order(mine, got)] if mine else []),
                *([{"level": "info", "text": f"{len(uploaded) - len(mine)} 段是手動上傳的，不比上傳時間的順序", "rows": []}]
                  if len(mine) < len(uploaded) else [])]

    @staticmethod
    def _row(it, vid=None):
        return {"label": f"{it['title']}（{' + '.join(it['files'])}）",
                "url": f"https://studio.youtube.com/video/{vid}/edit" if vid else ""}

    def _verify_counts(self):
        items = self._items
        up = sum(it["status"] == "uploaded" for it in items)
        hand = sum(it["status"] == "uploaded" and bool(it.get("hand")) for it in items)
        left = [it for it in items if it["status"] != "uploaded"]
        known = sum(bool(it["on_channel"]) and not it["on_channel_some"] for it in left)
        drafts = sum(bool(it["on_channel"]) and not it["on_channel_some"] and it.get("on_channel_draft") for it in left)
        text = f"這個資料夾 {len(items)} 段錄影：已上傳 {up} 段"
        text += f"（其中 {hand} 段是手動上傳的）" if hand else ""
        text += f"，還沒傳 {len(left) - known} 段"
        if drafts:
            text += f"，另有 {drafts} 段在頻道上是草稿（沒填設定，可以用比對結果裡的「修正手動上傳的影片…」補上）"
        if known - drafts:
            text += f"，另有 {known - drafts} 段頻道上已經有了（只有大小相同、檔名不同，沒有記為已上傳）"
        return {"level": "ok" if not left else "info", "text": text, "rows": []}

    def _verify_videos(self, uploaded, got):
        gone, stuck, differ, merged = [], [], [], 0
        for it in uploaded:
            sizes = it["sizes"] if len(it["sizes"]) == len(it["video_ids"]) else None
            merged += sizes is None
            for n, vid in enumerate(it["video_ids"]):
                v = got.get(vid)
                if not v:
                    gone.append(self._row(it, vid))
                    continue
                if v["upload_status"] != "processed":
                    stuck.append({**self._row(it, vid), "note": v["problem"] or v["upload_status"] or ""})
                if sizes and v["size"] and v["size"] != sizes[n]:
                    differ.append(self._row(it, vid))
        n = sum(len(it["video_ids"]) for it in uploaded)
        same = f"，大小和本機相同（{merged} 段合併過的不比大小）" if merged else "，大小和本機相同"
        return [
            {"level": "bad", "text": f"{len(gone)} 支在 YouTube 上找不到（可能已刪除），可以重新勾選上傳", "rows": gone} if gone
            else {"level": "ok", "text": f"{n} 支都在 YouTube 上" + ("" if differ else same), "rows": []},
            *([{"level": "bad", "text": f"{len(differ)} 支的大小和本機不同，可能不是同一個檔案", "rows": differ}] if differ else []),
            *([{"level": "warn", "text": f"{len(stuck)} 支 YouTube 還沒處理好（剛傳完的要等一下；失敗的要重傳）", "rows": stuck}] if stuck else []),
        ]

    def _verify_playlist(self, yt, uploaded):
        """Against the playlist each video was uploaded for, not the one picked right now."""
        s, used = self._settings, self._store.playlists_used()
        groups: dict[str, list] = {}
        for it in uploaded:
            name = used.get(it["video_ids"][0], s.playlist if s.playlist_id else "")
            if name:
                groups.setdefault(name, []).append(it)
        if not groups:
            return [{"level": "info", "text": "這些影片上傳時沒有選播放清單，所以沒有核對", "rows": []}]
        others = [] if set(groups) <= {s.playlist} else youtube.list_playlists(yt)
        checks = []
        for name, its in groups.items():
            pid = s.playlist_id if name == s.playlist and s.playlist_id else next(
                (p["id"] for p in others if p["title"] == name), None)
            checks += (self._verify_one_playlist(yt, pid, f"播放清單「{name}」", its) if pid else
                       [{"level": "bad", "text": f"找不到播放清單「{name}」，可能改名或刪除了，裡面的 {len(its)} 段沒有核對", "rows": []}])
        return checks

    def _verify_one_playlist(self, yt, playlist_id, name, uploaded):
        try:
            ids = youtube.playlist_videos(yt, playlist_id)
            alive = youtube.file_info(yt, ids)
        except youtube.SignedOut:
            raise
        except Exception as e:  # noqa: BLE001 - e.g. deleted: the other checks still count
            log.warning("could not read the playlist: %s", e)
            return [{"level": "bad", "text": f"讀不到{name}，可能已刪除", "rows": []}]
        where = {v: k for k, v in enumerate(ids)}
        out_rows, positions = [], []
        for it in uploaded:
            for vid in it["video_ids"]:
                if vid in where:
                    positions.append((where[vid], it))
                else:
                    out_rows.append(self._row(it, vid))
        waiting = sum(bool((self._store.lookup(it) or {}).get("playlist_add")) for it in uploaded)
        dead = [v for v in ids if v not in alive]
        wrong = next((it for (p, _), (q, it) in zip(positions, positions[1:]) if q < p), None)
        checks = []
        if out_rows:
            note = f"（{waiting} 段在等下次上傳時補加入）" if waiting else ""
            checks.append({"level": "bad" if not waiting else "warn",
                           "text": f"{len(out_rows)} 支不在{name}{note}", "rows": out_rows})
        else:
            checks.append({"level": "ok", "text": f"都在{name}裡（清單共 {len(ids)} 支）", "rows": []})
        sortable = playlist_id == self._settings.playlist_id and not self._settings.no_playlist
        checks.append({"level": "bad", "text": f"{name}的順序不對，從這段開始和表格不同", "rows": [self._row(wrong)],
                       **({"action": {"call": "sort_playlist", "label": "排好順序（每移動一支約 50 配額單位）"}} if sortable else {})}
                      if wrong else {"level": "ok", "text": f"{name}的順序和表格相同", "rows": []})
        if dead:
            checks.append({"level": "warn", "text": f"{name}裡有 {len(dead)} 個已刪除或看不到的影片，要到 YouTube 上移除",
                           "rows": [{"label": v, "url": f"https://www.youtube.com/playlist?list={playlist_id}"} for v in dead[:20]]})
        return checks

    @staticmethod
    def _verify_order(uploaded, got):
        """Studio lists by upload time, which videos.list gives to the second."""
        seen = [(got[it["video_ids"][0]]["published"], it) for it in uploaded if it["video_ids"][0] in got]
        wrong = [Api._row(it, it["video_ids"][0]) for (a, _), (b, it) in zip(seen, seen[1:]) if b < a]
        return ({"level": "warn", "text": f"上傳時間的順序和表格不同的有 {len(wrong)} 處（Studio 依這個時間排序）", "rows": wrong} if wrong
                else {"level": "ok", "text": "上傳時間的順序和表格相同", "rows": []})

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
        done, sent = 0, {}  # video id -> the snippet YouTube answered with
        for k, it in enumerate(todo, 1):
            fields = set(it["yt_pending"])
            vids = it["video_ids"]
            self._emit("push", done=k, total=len(todo), title=it["title"])
            try:
                for n, vid in enumerate(vids, 1):
                    title = it["title"] + (f" ({n}/{len(vids)})" if len(vids) > 1 else "")
                    sent[vid] = self._with_sign_in(lambda yt, v=vid, t=title: youtube.update_snippet(
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
            if it.get("hand"):
                self._note_by_app(it, "pushed")
            done += 1
            self._say(f"✓ 已更新：{it['title']}")
            yt_date = ((it.get("yt") or [{}])[0] or {}).get("recording_date")
            if yt_date and yt_date != it["recording_date"][:10]:
                self._say(f"  拍攝日期 {it['recording_date'][:10]} 要到 Studio 改（YouTube 上是 {yt_date}）")
        self._say(f"已更新 {done} / {len(todo)} 支")
        self._sync_youtube()  # read back what YouTube has now
        # a read right after an update can still give the old title (seen in real use):
        # what the update answered is what YouTube has
        for it in todo:
            rec = self._store.lookup(it) or {}
            saved = dict(rec.get("yt") or {})
            fresh = {v: {**saved[v], "title": sent[v].get("title", saved[v].get("title")),
                         "description": sent[v].get("description", saved[v].get("description"))}
                     for v in it["video_ids"] if v in sent and saved.get(v)}
            if fresh:
                self._store.update(it, yt={**saved, **fresh})
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
        self._adopt_hand()  # by the last comparison, if there is one; reads nothing
        self._backfill_by_app()
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

    def fix_title_times(self, ids: list[str]):
        """Uploaded videos whose title starts with the wrong date / time: put the file's there,
        keep the rest of the title, and mark it for 更新到 YouTube."""
        by_id = {it["id"]: it for it in self._items}
        for i in ids:
            it = by_id.get(i)
            if it and it.get("title_should") and not self._locked(i):
                self.edit([i], {"title": it["title_should"]})
        return self._replan()

    def title_from_previous(self, item_id: str):
        """Take the row above's title, with this recording's own date / time at its start
        (copying by hand easily keeps the old time)."""
        order = self._items
        k = next((n for n, it in enumerate(order) if it["id"] == item_id), None)
        if not k or self._locked(item_id):
            return self._replan()
        it, prev = order[k], order[k - 1]
        new = retime_title(prev["title"], datetime.fromisoformat(it["local"]), it["time_known"])
        self.edit([item_id], {"title": new})
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
        self._forget_channel()
        self._yt = youtube.get_service(interactive=True)
        self._told_signed_out = False
        name = youtube.channel_title(self._yt)
        self._say(f"已登入 YouTube 頻道：{name}")
        return name

    def reopen_sign_in(self):
        return youtube.reopen_sign_in()

    def cancel_sign_in(self):
        youtube.cancel_sign_in()
        return True

    def sign_out(self):
        youtube.sign_out()
        self._yt = None
        self._forget_channel()
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
        """Open the file manager at the file's folder with the file selected."""
        p = self._scanned_file(item_id, index)
        if not p:
            return False
        subprocess.Popen(reveal_command(p))  # noqa: S603
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
        subprocess.Popen(reveal_command(p))  # noqa: S603
        return True

    def open_photo(self, folder: str, name: str):
        p = self._scanned_photo(folder, name)
        if not p:
            return False
        open_with_default_app(p)
        return True

    def open_original(self, item_id: str, index: int = 0):
        """Play the original (full-resolution) file in the default player."""
        p = self._scanned_file(item_id, index)
        if not p:
            return False
        open_with_default_app(p)
        return True

    def open_path(self, which: str):
        target = {"data": config.app_dir(), "log": self._log_file,
                  "history": config.path("upload_history.csv")}.get(which)
        if target and os.path.exists(target):
            open_with_default_app(target)
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
            return (f"正在等 YouTube 配額重置（{self._quota_until:%H:%M} 再試一次）。\n\n"
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
        # a ticked draft (on YouTube already, nothing filled in) is for 修正, never uploaded again
        todo = [i for i in self._items if i["include"] and i["status"] != "uploaded" and not i.get("on_channel_draft")]
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
        self._quota_hit.clear()
        self._cut = set()
        ok, failed, hold, signed_out, halted = 0, False, False, None, False
        quota_until = None             # local time to try again, while waiting for the quota
        quota_said = None              # the reset time last told to the user

        def quota_stop(msg):
            nonlocal quota_until, quota_said
            if quota_until:
                return
            reset = quota_reset()
            quota_until = min(reset, datetime.now() + QUOTA_PROBE)
            if reset != quota_said:  # once, not after every hourly try
                quota_said = reset
                self._say(msg.format(reset=reset), "error")

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
                        self._say("配額已重置，繼續上傳" if quota_until >= quota_said else "試試看配額恢復了沒")
                        quota_until, hold = None, False
                        self._quota_hit.clear()
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
                        quota_stop("YouTube 今日配額用完，正在傳的會傳完；每小時試一次，"
                                   "最晚 {reset:%H:%M} 配額重置後自動繼續（程式開著就好）")
                        hold = True
                    except youtube.Cancelled:
                        self._set_status(item, "pending")
                    except Exception as e:  # noqa: BLE001
                        log.exception("upload failed")
                        self._set_status(item, "error", error=str(e))
                        self._say(f"失敗：{item['title']}：{e}", "error")
                        self._taskbar.state(taskbar.ERROR)
                        # the ones before it go on; the ones after it stop so it can be redone
                        # before they finish (Studio lists by upload date) and resume next time
                        after = {i["id"] for i in running.values() if order[i["id"]] > order[item["id"]]}
                        if not halted:
                            self._say("已暫停後面的，保持影片順序：排在它前面的會傳完，後面正在傳的"
                                      "下次開始上傳時從中斷處接著傳。", "error")
                        self._cut |= after
                        failed, halted, hold = True, True, True
                        waiting.clear()
                try:  # in file order, as far as the earlier ones are done
                    self._flush_playlist(yt, held=waiting if signed_out else ())
                except youtube.QuotaExceeded:
                    quota_stop("配額用完，有些影片還沒加入播放清單；每小時試一次，"
                               "最晚 {reset:%H:%M} 配額重置後自動補上並繼續")
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
        rank, listed = self._playlist_rank(), {}
        for first, fp, add in ready:
            if first_busy and first > first_busy:
                break  # an earlier recording is still uploading: keep the order
            ids = list(add["ids"])
            if add["playlist"] not in listed:  # its place among what is there (an older trip's later videos…)
                listed[add["playlist"]] = youtube.playlist_videos(yt, add["playlist"])
            while ids:
                if ids[0] not in listed[add["playlist"]]:
                    self._add_in_place(yt, add["playlist"], ids[0], rank, listed[add["playlist"]])
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
                return [(merge.merge(paths, cancel=lambda: self._stopping(item)), title, True)]
            except RuntimeError:
                if self._stopping(item):  # stopped while merging: not a failure, just not done yet
                    raise youtube.Cancelled() from None
                raise
        return [(p, f"{title} ({i}/{len(paths)})", False) for i, p in enumerate(paths, 1)]

    def _taskbar_progress(self, item_id, sent):
        self._sent[item_id] = sent
        self._taskbar.progress(sum(self._sent.values()) / self._batch_bytes)

    def _stopping(self, item) -> bool:
        return self._cancel or item["id"] in self._cut

    def _upload_item(self, yt, item, chain=None):
        """Upload one recording (all its parts). Runs in a pool thread.

        chain = (prev_started, started, prev_done, done) keeps the table order (see _upload_all).
        """
        prev_started, started, prev_done, done = chain or (None, None, None, None)
        failed = False
        try:
            self._upload_parts(yt, item, prev_started, started, prev_done)
        except youtube.QuotaExceeded:
            self._quota_hit.set()
            raise
        except (youtube.Cancelled, youtube.SignedOut):
            raise
        except Exception:
            # the next one must not send its last chunk now; _upload_all stops it instead
            failed = True
            raise
        finally:
            self._parked.discard(item["id"])
            for ev in (started, None if failed else done):
                if ev:
                    ev.set()

    def _upload_parts(self, yt, item, prev_started, started, prev_done):
        s = self._settings
        prev = self._store.lookup(item) or {}
        done_ids = prev.get("video_ids", []) if prev.get("status") == "partial" else []
        item["video_ids"] = list(done_ids)
        sources = self._sources(item)
        while prev_started and not prev_started.wait(1):
            if self._stopping(item):
                raise youtube.Cancelled()
        if prev_started and self._quota_hit.is_set():
            raise youtube.QuotaExceeded("前一支被配額擋下，先不試")
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
                while not prev_done.wait(1) and not self._stopping(item):
                    pass
                self._parked.discard(item["id"])
                waited = time.time() - w0

            vid = youtube.upload(yt, path, body, progress=progress, cancel=lambda: self._stopping(item),
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


def reveal_command(path: str, platform: str = sys.platform) -> list[str]:
    """The command that shows a file selected in the system's file manager."""
    if platform == "win32":
        return ["explorer", f"/select,{os.path.normpath(path)}"]
    if platform == "darwin":
        return ["open", "-R", path]
    return ["xdg-open", os.path.dirname(path)]  # Linux file managers have no common "select"


def open_with_default_app(path: str) -> None:
    """Open a file or folder the way a double-click would (user-initiated only)."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])  # noqa: S603,S607


def _ask_yes_no(window, text: str) -> bool:
    """A Windows message box owned by the window (No is the default button);
    pywebview's own dialog elsewhere (Mac / Linux: not tried on a real machine)."""
    if sys.platform != "win32":
        return bool(window.create_confirmation_dialog(WINDOW_TITLE, text))
    try:
        hwnd = window.native.Handle.ToInt32()
    except Exception:  # noqa: BLE001 - no handle: a task-modal box works too
        hwnd = 0
    flags = 0x4 | 0x30 | 0x100 | (0 if hwnd else 0x2000)  # YESNO | WARNING | DEFBUTTON2 | TASKMODAL
    return ctypes.windll.user32.MessageBoxW(hwnd, text, WINDOW_TITLE, flags) == 6  # IDYES
