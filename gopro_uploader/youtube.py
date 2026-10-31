"""YouTube Data API v3: sign-in, playlists and resumable uploads."""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import time

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from . import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
CHUNK = 32 * 1024 * 1024
RETRY_STATUS = {429, 500, 502, 503, 504}


class QuotaExceeded(Exception):
    pass


class Cancelled(Exception):
    pass


class SignedOut(Exception):
    """The saved sign-in no longer works (expired after 7 days in "testing", revoked, …)."""


def _is_signed_out(e: Exception) -> bool:
    if isinstance(e, RefreshError):
        return True
    return isinstance(e, HttpError) and e.resp.status == 401


def client_secret_path() -> str:
    return config.path("client_secret.json")


def has_client_secret() -> bool:
    return os.path.exists(client_secret_path())


def install_client_secret(src: str) -> None:
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    if "installed" not in data:
        raise ValueError("這不是「桌面應用程式」類型的 OAuth 用戶端 JSON")
    shutil.copyfile(src, client_secret_path())


def _token_path():
    return config.path("token.json")


def has_saved_login() -> bool:
    """A token is stored (it may still be expired: projects in "testing" lose it after 7 days)."""
    return os.path.exists(_token_path())


def sign_out():
    if os.path.exists(_token_path()):
        os.remove(_token_path())


def get_service(interactive: bool = True):
    creds = None
    if os.path.exists(_token_path()):
        creds = Credentials.from_authorized_user_file(_token_path(), SCOPES)
    if creds and creds.refresh_token and (creds.expired or not interactive):
        # Always try a refresh on a silent start: an access token can look valid
        # for an hour after the refresh token itself has already been revoked.
        try:
            creds.refresh(Request())
        except RefreshError as e:  # Google says no: expired (7 days in "testing") or revoked
            log.warning("saved sign-in no longer works: %s", e)
            if not interactive:
                raise SignedOut(str(e)) from e
            creds = None
        except Exception as e:  # noqa: BLE001 - offline etc.: not signed out, just unreachable
            log.warning("could not refresh the sign-in (offline?): %s", e)
            if not interactive:
                return None
            raise
    if not creds or not creds.valid:
        if not interactive:
            return None
        if not has_client_secret():
            raise RuntimeError("尚未設定 client_secret.json")
        flow = InstalledAppFlow.from_client_secrets_file(client_secret_path(), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True,
                                      success_message="登入完成，可以關閉這個分頁回到程式。")
    with open(_token_path(), "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def channel_title(yt) -> str:
    r = _execute(yt.channels().list(part="snippet", mine=True))
    items = r.get("items", [])
    return items[0]["snippet"]["title"] if items else "(沒有頻道)"


def list_playlists(yt) -> list[dict]:
    """The channel's playlists, newest first (1 quota unit per 50)."""
    out = []
    req = yt.playlists().list(part="snippet,status,contentDetails", mine=True, maxResults=50)
    while req is not None:
        resp = _execute(req)
        for p in resp.get("items", []):
            out.append({"id": p["id"], "title": p["snippet"]["title"],
                        "count": p.get("contentDetails", {}).get("itemCount", 0),
                        "privacy": p.get("status", {}).get("privacyStatus"),
                        "published": p["snippet"].get("publishedAt", "")})
        req = yt.playlists().list_next(req, resp)
    return sorted(out, key=lambda p: p["published"], reverse=True)


def create_playlist(yt, title: str, privacy: str = "unlisted") -> dict:
    body = {"snippet": {"title": title}, "status": {"privacyStatus": privacy}}
    p = _execute(yt.playlists().insert(part="snippet,status", body=body))
    return {"id": p["id"], "title": p["snippet"]["title"], "count": 0,
            "privacy": p.get("status", {}).get("privacyStatus", privacy),
            "published": p["snippet"].get("publishedAt", "")}


def add_to_playlist(yt, playlist_id: str, video_id: str) -> None:
    body = {"snippet": {"playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    _execute(yt.playlistItems().insert(part="snippet", body=body))


def video_body(title, description, category_id, privacy, recording_date,
               title_language=None, audio_language=None) -> dict:
    """No location: recordingDetails.location / locationDescription are deprecated (2017-06-01)
    and silently ignored on insert and update (Google: "Won't Fix", issuetracker 268030207;
    confirmed on real uploads 2026-10). The location is set by hand in Studio."""
    snippet = {"title": title[:100], "description": description[:5000], "categoryId": category_id}
    if title_language:
        snippet["defaultLanguage"] = title_language
    if audio_language:
        snippet["defaultAudioLanguage"] = audio_language
    body = {
        "snippet": snippet,
        "status": {"privacyStatus": privacy,
                   "selfDeclaredMadeForKids": False,     # 不是為兒童打造
                   "containsSyntheticMedia": False},     # 非 AI / 合成內容
        "paidProductPlacementDetails": {"hasPaidProductPlacement": False},  # 無付費宣傳
        "recordingDetails": {"recordingDate": recording_date},
    }
    return body


def update_snippet(yt, video_id: str, title: str | None = None, description: str | None = None) -> dict:
    """Change the title and/or description of an uploaded video (about 50 quota units).

    videos.update replaces the whole snippet, so the current one is read first and only
    the given fields are changed (category, languages and tags are kept). Returns the new snippet.
    """
    items = _execute(yt.videos().list(id=video_id, part="snippet")).get("items") or []
    if not items:
        raise ValueError(f"YouTube 上找不到影片 {video_id}")
    sn = items[0]["snippet"]
    body = {k: sn[k] for k in ("title", "description", "categoryId", "tags",
                               "defaultLanguage", "defaultAudioLanguage") if k in sn}
    if title is not None:
        body["title"] = title[:100]
    if description is not None:
        body["description"] = description[:5000]
    return _execute(yt.videos().update(part="snippet", body={"id": video_id, "snippet": body}))["snippet"]


def fetch_videos(yt, video_ids: list[str]) -> dict[str, dict]:
    """What YouTube has now for these videos (1 quota unit per 50). Missing ids are left out."""
    out = {}
    ids = list(dict.fromkeys(video_ids))
    for k in range(0, len(ids), 50):
        r = _execute(yt.videos().list(id=",".join(ids[k:k + 50]),
                                      part="snippet,status,recordingDetails", maxResults=50))
        for v in r.get("items", []):
            sn, rd = v.get("snippet", {}), v.get("recordingDetails", {})
            loc = rd.get("location") or {}
            out[v["id"]] = {
                "title": sn.get("title", ""),
                "description": sn.get("description", ""),
                "privacy": v.get("status", {}).get("privacyStatus"),
                "recording_date": (rd.get("recordingDate") or "")[:10],
                "place": rd.get("locationDescription", ""),
                "lat": loc.get("latitude"), "lon": loc.get("longitude"),
                "title_language": sn.get("defaultLanguage"),
                "audio_language": sn.get("defaultAudioLanguage"),
            }
    return out


def file_info(yt, video_ids: list[str]) -> dict[str, dict]:
    """Original file size and name and YouTube's processing state (1 quota unit per 50).

    fileDetails is only given to the video's owner. Uploads through this API have no file name,
    but the size is the exact byte count of the file sent. Missing ids are left out."""
    out = {}
    ids = list(dict.fromkeys(video_ids))
    for k in range(0, len(ids), 50):
        r = _execute(yt.videos().list(id=",".join(ids[k:k + 50]), part="snippet,status,fileDetails", maxResults=50))
        for v in r.get("items", []):
            sn, st, fd = v.get("snippet", {}), v.get("status", {}), v.get("fileDetails", {})
            out[v["id"]] = {
                "id": v["id"], "title": sn.get("title", ""), "published": sn.get("publishedAt", ""),
                "size": int(fd["fileSize"]) if fd.get("fileSize") else None, "name": fd.get("fileName", ""),
                "privacy": st.get("privacyStatus"), "upload_status": st.get("uploadStatus"),
                "problem": st.get("failureReason") or st.get("rejectionReason") or "",
            }
    return out


def uploads_playlist(yt) -> str | None:
    """The channel's "all uploads" playlist (1 quota unit). Its itemCount is not used: it was
    far below the real number of videos on a channel tried."""
    items = _execute(yt.channels().list(part="contentDetails", mine=True)).get("items", [])
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"] if items else None


def read_playlist(yt, playlist_id: str, since: str = "", on_page=None, cancel=None) -> tuple[list[dict], bool]:
    """The videos in a playlist with file_info, in its order (about 2 quota units per 50).

    since (ISO time) is for the uploads playlist, which runs newest first: reading stops at the
    first page entirely older than that. Returns (videos, read to the end); a cancel keeps what
    was read so far."""
    out = []
    req = yt.playlistItems().list(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50)
    while req is not None:
        if cancel and cancel():
            return out, False
        resp = _execute(req)
        rows = resp.get("items", [])
        info = file_info(yt, [r["contentDetails"]["videoId"] for r in rows])
        out += [v for r in rows if (v := info.get(r["contentDetails"]["videoId"])) and v["published"] >= since]
        if on_page:
            on_page(len(out))
        if since and rows and all(r["snippet"].get("publishedAt", "") < since for r in rows):
            break
        req = yt.playlistItems().list_next(req, resp)
    return out, True


def playlist_videos(yt, playlist_id: str) -> list[str]:
    """The video ids in a playlist, in its order (1 quota unit per 50)."""
    out = []
    req = yt.playlistItems().list(part="contentDetails", playlistId=playlist_id, maxResults=50)
    while req is not None:
        resp = _execute(req)
        out += [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
        req = yt.playlistItems().list_next(req, resp)
    return out


def service_copy(yt):
    """A separate service object for another upload thread (httplib2 is not thread-safe)."""
    return build("youtube", "v3", credentials=yt._http.credentials, cache_discovery=False)


def upload(yt, path: str, body: dict, progress=None, cancel=None,
           resume_uri: str | None = None, on_session=None, before_last=None) -> str:
    """Resumable upload. on_session(uri) gets the upload session URI once YouTube has opened it;
    passing it back as resume_uri (even after a restart, for about a week) continues where it
    stopped instead of sending the whole file again. before_last() is called (once) right
    before the chunk that completes the video."""
    if resume_uri:
        try:
            return _upload(yt, path, body, progress, cancel, resume_uri, on_session, before_last)
        except HttpError as e:
            if e.resp.status not in (400, 404, 410):
                raise
            log.warning("could not resume (session gone?), starting over: %s", e)
    try:
        return _upload(yt, path, body, progress, cancel, None, on_session, before_last)
    except HttpError as e:
        # If the API ever rejects the paid-promotion part, it fails before any
        # bytes are sent; retry without it (YouTube defaults it to "no").
        if e.resp.status == 400 and "paidProductPlacementDetails" in body:
            log.warning("retrying without paidProductPlacementDetails: %s", e)
            body = {k: v for k, v in body.items() if k != "paidProductPlacementDetails"}
            return _upload(yt, path, body, progress, cancel, None, on_session, before_last)
        raise


def _upload(yt, path, body, progress, cancel, resume_uri=None, on_session=None, before_last=None) -> str:
    parts = ",".join(body.keys())
    media = MediaFileUpload(path, mimetype="video/*", chunksize=CHUNK, resumable=True)
    req = yt.videos().insert(part=parts, body=body, media_body=media, notifySubscribers=False)
    if resume_uri:
        # googleapiclient's own recovery path: ask the server how much it already has
        # ("Content-Range: bytes */size"), then carry on from there
        req.resumable_uri = resume_uri
        req._in_error_state = True
    told = bool(resume_uri)
    response, retry = None, 0
    while response is None:
        if cancel and cancel():
            raise Cancelled()
        if before_last and media.size() - req.resumable_progress <= CHUNK:
            # the next chunk completes the video: the caller may hold it back to fix the
            # order videos are finished in (and so their order in Studio)
            before_last()
            before_last = None
            if cancel and cancel():
                raise Cancelled()
        try:
            status, response = req.next_chunk()
            retry = 0
            if not told and req.resumable_uri and on_session:
                on_session(req.resumable_uri)
                told = True
            if status and progress:
                progress(status.resumable_progress, status.total_size)
        except (HttpError, RefreshError) as e:
            if _is_signed_out(e):
                raise SignedOut(str(e)) from e
            _raise_if_quota(e)
            if e.resp.status not in RETRY_STATUS:
                raise
            retry = _backoff(retry, e)
        except (ConnectionError, TimeoutError, OSError) as e:
            retry = _backoff(retry, e)
    if progress:
        progress(media.size(), media.size())
    return response["id"]


def _backoff(retry, err):
    retry += 1
    if retry > 10:
        raise RuntimeError(f"重試太多次仍失敗：{err}")
    sleep = min(2 ** retry, 120) + random.random()
    log.warning("upload error %s, retry %d in %.0fs", err, retry, sleep)
    time.sleep(sleep)
    return retry


def _raise_if_quota(e: HttpError):
    try:
        err = json.loads(e.content)["error"]
        reasons = [x.get("reason") for x in err["errors"]]
        message = str(err.get("message", ""))
    except Exception:  # noqa: BLE001
        reasons, message = [], ""
    if any(r in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded") for r in reasons):
        raise QuotaExceeded(", ".join(reasons))
    # the Cloud project's own daily cap (429 "Quota exceeded for quota metric 'Video Uploads'
    # and limit 'Video Uploads per day'", seen after 88 uploads in a day); a plain
    # rateLimitExceeded without "per day" is short-term and only retried
    if "rateLimitExceeded" in reasons and "per day" in message:
        raise QuotaExceeded("rateLimitExceeded (Video Uploads per day)")


def _execute(req):
    try:
        return req.execute(num_retries=3)
    except (HttpError, RefreshError) as e:
        if _is_signed_out(e):
            raise SignedOut(str(e)) from e
        _raise_if_quota(e)
        raise
