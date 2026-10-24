"""YouTube Data API v3: sign-in, playlists and resumable uploads."""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import time

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
RETRY_STATUS = {500, 502, 503, 504}


class QuotaExceeded(Exception):
    pass


class Cancelled(Exception):
    pass


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
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:  # noqa: BLE001 - token revoked etc.
            creds = None
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
    r = yt.channels().list(part="snippet", mine=True).execute()
    items = r.get("items", [])
    return items[0]["snippet"]["title"] if items else "(沒有頻道)"


def find_or_create_playlist(yt, title: str, privacy: str = "unlisted") -> str:
    req = yt.playlists().list(part="snippet", mine=True, maxResults=50)
    while req is not None:
        resp = _execute(req)
        for p in resp.get("items", []):
            if p["snippet"]["title"] == title:
                return p["id"]
        req = yt.playlists().list_next(req, resp)
    body = {"snippet": {"title": title}, "status": {"privacyStatus": privacy}}
    return _execute(yt.playlists().insert(part="snippet,status", body=body))["id"]


def add_to_playlist(yt, playlist_id: str, video_id: str) -> None:
    body = {"snippet": {"playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    _execute(yt.playlistItems().insert(part="snippet", body=body))


def video_body(title, description, category_id, privacy, recording_date,
               lat=None, lon=None, place=None, title_language=None, audio_language=None) -> dict:
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
    if lat is not None and lon is not None:
        body["recordingDetails"]["location"] = {"latitude": lat, "longitude": lon}
    if place:
        body["recordingDetails"]["locationDescription"] = place
    return body


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


def set_location(yt, video_id: str, recording_date: str, lat, lon, place) -> bool:
    """Write the location with videos.update and read it back.

    videos.insert accepted recordingDetails.location/locationDescription but YouTube
    did not keep them, while a location set in Studio
    shows up in exactly those fields. So we set it again after the upload.
    update replaces the whole part, hence recordingDate is sent too.
    """
    details = {"recordingDate": recording_date,
               "location": {"latitude": lat, "longitude": lon}}
    if place:
        details["locationDescription"] = place
    _execute(yt.videos().update(part="recordingDetails",
                                body={"id": video_id, "recordingDetails": details}))
    r = _execute(yt.videos().list(id=video_id, part="recordingDetails"))
    got = (r.get("items") or [{}])[0].get("recordingDetails", {})
    return "location" in got


def upload(yt, path: str, body: dict, progress=None, cancel=None) -> str:
    try:
        return _upload(yt, path, body, progress, cancel)
    except HttpError as e:
        # If the API ever rejects the paid-promotion part, it fails before any
        # bytes are sent; retry without it (YouTube defaults it to "no").
        if e.resp.status == 400 and "paidProductPlacementDetails" in body:
            log.warning("retrying without paidProductPlacementDetails: %s", e)
            body = {k: v for k, v in body.items() if k != "paidProductPlacementDetails"}
            return _upload(yt, path, body, progress, cancel)
        raise


def _upload(yt, path, body, progress, cancel) -> str:
    parts = ",".join(body.keys())
    media = MediaFileUpload(path, mimetype="video/*", chunksize=CHUNK, resumable=True)
    req = yt.videos().insert(part=parts, body=body, media_body=media, notifySubscribers=False)
    response, retry = None, 0
    while response is None:
        if cancel and cancel():
            raise Cancelled()
        try:
            status, response = req.next_chunk()
            retry = 0
            if status and progress:
                progress(status.resumable_progress, status.total_size)
        except HttpError as e:
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
        reasons = [x.get("reason") for x in json.loads(e.content)["error"]["errors"]]
    except Exception:  # noqa: BLE001
        reasons = []
    if any(r in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded") for r in reasons):
        raise QuotaExceeded(", ".join(reasons))


def _execute(req):
    try:
        return req.execute(num_retries=3)
    except HttpError as e:
        _raise_if_quota(e)
        raise
