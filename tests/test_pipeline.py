import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from gopro_uploader import config, scanner
from gopro_uploader.mp4meta import parse_gpmf_gps, read_mp4
from gopro_uploader.planner import build_plan
from gopro_uploader.scanner import group_key, scan
from tests.fake_gopro import gps5_payload, gps9_payload, make_mp4

# Camera clock set to Japan time; GPS says 05:32:10 UTC = 14:32:10 JST
CAM = datetime(2026, 3, 15, 14, 32, 10)
SAMPLE_PLACE = (10.123456, 20.654321)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GOPRO_YT_HOME", str(tmp_path / "home"))


def test_group_key():
    assert group_key("GX010123.MP4") == ("GX0123", 1)
    assert group_key("GX020123.MP4") == ("GX0123", 2)
    assert group_key("GOPR0042.MP4") == ("GOPR0042", 0)
    assert group_key("GP010042.MP4") == ("GOPR0042", 1)
    assert group_key("holiday.mp4") == ("holiday", 0)


def test_gps9_payload():
    days = (datetime(2026, 3, 15) - datetime(2000, 1, 1)).days
    lat, lon, utc, fix, dop = parse_gpmf_gps(gps9_payload(*SAMPLE_PLACE, days, 5 * 3600 + 32 * 60 + 10.5))
    assert lat == pytest.approx(SAMPLE_PLACE[0], abs=1e-6)
    assert lon == pytest.approx(SAMPLE_PLACE[1], abs=1e-6)
    assert utc == datetime(2026, 3, 15, 5, 32, 10, 500000, tzinfo=timezone.utc)
    assert fix == 3 and dop == pytest.approx(1.2)


def test_read_mp4_skips_samples_without_fix(tmp_path):
    p = tmp_path / "GX010001.MP4"
    samples = [gps5_payload(0, 0, "260315053210.000", fix=0)] * 3 + \
              [gps5_payload(*SAMPLE_PLACE, "260315053213.000")] * 5
    make_mp4(p, CAM, 8.0, samples)
    info = read_mp4(str(p))
    assert info.camera_time == CAM
    assert info.duration == 8.0
    fix = info.first_fix
    assert fix.lat == pytest.approx(SAMPLE_PLACE[0], abs=1e-6)
    assert fix.t == 3.0
    assert fix.utc == datetime(2026, 3, 15, 5, 32, 13, tzinfo=timezone.utc)


def test_plan_end_to_end(tmp_path):
    # recording 1: two chapters with GPS; recording 2: no GPS, 20 minutes later
    gps = [gps5_payload(*SAMPLE_PLACE, "260315053210.000")] * 4
    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, gps)
    make_mp4(tmp_path / "GX020001.MP4", CAM.replace(minute=40), 4.0,
             [gps5_payload(*SAMPLE_PLACE, "260315054010.000")] * 4)
    make_mp4(tmp_path / "GX010002.MP4", CAM.replace(minute=52), 60.0, [])
    (tmp_path / "GX010001.LRV").write_bytes(b"x")
    (tmp_path / "GX010001.THM").write_bytes(b"x")

    recs = scan(str(tmp_path))
    assert [len(r.clips) for r in recs] == [2, 1]

    s = config.Settings(camera_tz="auto", trip_tz="Asia/Tokyo")
    items = build_plan(recs, s, {})
    a, b = items
    assert a["files"] == ["GX010001.MP4", "GX020001.MP4"]
    assert a["title"] == "2026-03-15 14:32 JST"
    assert a["time_source"] == "gps"
    assert a["description"] == "GX010001.MP4\nGX020001.MP4"
    assert a["recording_date"] == "2026-03-15T00:00:00Z"
    assert a["loc_source"] == "gps"
    # no GPS: time from camera clock corrected by the GPS-derived offset, place inferred
    assert b["title"] == "2026-03-15 14:52 JST"
    assert b["time_source"] == "camera+gps_offset"
    assert b["loc_source"] == "nearby"
    assert b["lat"] == pytest.approx(SAMPLE_PLACE[0], abs=1e-6)


def test_camera_clock_in_taiwan_time_is_corrected(tmp_path):
    # camera still on Taiwan time (1h behind Japan) -> titles should be Japan local
    tw_cam = CAM.replace(hour=13)
    make_mp4(tmp_path / "GX010001.MP4", tw_cam, 4.0, [gps5_payload(*SAMPLE_PLACE, "260315053210.000")] * 4)
    make_mp4(tmp_path / "GX010002.MP4", tw_cam.replace(minute=50), 4.0, [])
    items = build_plan(scan(str(tmp_path)), config.Settings(), {})
    assert [i["title"] for i in items] == ["2026-03-15 14:32 JST", "2026-03-15 14:50 JST"]


@pytest.fixture
def count_parses(monkeypatch):
    calls = []

    def counting(path):
        calls.append(os.path.basename(path))
        return read_mp4(path)

    monkeypatch.setattr(scanner, "read_mp4", counting)
    return calls


def test_scan_cache_hit_skips_parsing(tmp_path, count_parses):
    vids = tmp_path / "vids"
    vids.mkdir()
    make_mp4(vids / "GX010001.MP4", CAM, 4.0, [gps5_payload(*SAMPLE_PLACE, "260315053210.000")] * 4)
    make_mp4(vids / "GX010002.MP4", CAM.replace(minute=50), 4.0, [])
    first = scan(str(vids))
    assert sorted(count_parses) == ["GX010001.MP4", "GX010002.MP4"]
    assert os.path.exists(config.path("scan_cache.json"))

    count_parses.clear()
    second = scan(str(vids))
    assert count_parses == []
    assert [c.info for r in second for c in r.clips] == [c.info for r in first for c in r.clips]
    assert second[0].clips[0].info.first_fix.utc == datetime(2026, 3, 15, 5, 32, 10, tzinfo=timezone.utc)


def test_scan_cache_reparses_changed_file(tmp_path, count_parses):
    vids = tmp_path / "vids"
    vids.mkdir()
    a, b = vids / "GX010001.MP4", vids / "GX010002.MP4"
    make_mp4(a, CAM, 4.0, [])
    make_mp4(b, CAM.replace(minute=50), 4.0, [])
    scan(str(vids))

    # a: different content and size; b: same bytes, only mtime changes
    make_mp4(a, CAM.replace(minute=10), 8.0, [gps5_payload(*SAMPLE_PLACE, "260315053210.000")] * 4)
    st = os.stat(b)
    os.utime(b, (st.st_atime, st.st_mtime + 10))
    count_parses.clear()
    recs = scan(str(vids))
    assert sorted(count_parses) == ["GX010001.MP4", "GX010002.MP4"]
    assert recs[0].clips[0].info.duration == 8.0


def test_scan_cache_corrupt_file_is_rebuilt(tmp_path, count_parses):
    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, [])
    with open(config.path("scan_cache.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    scan(str(tmp_path))
    assert count_parses == ["GX010001.MP4"]
    count_parses.clear()
    scan(str(tmp_path))
    assert count_parses == []


def test_overrides_and_duplicate_titles(tmp_path):
    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, [])
    make_mp4(tmp_path / "GX010002.MP4", CAM.replace(second=40), 4.0, [])
    recs = scan(str(tmp_path))
    items = build_plan(recs, config.Settings(), {})
    assert [i["title"] for i in items] == ["2026-03-15 14:32 JST", "2026-03-15 14:32 JST (2)"]
    key = items[0]["id"]
    items = build_plan(recs, config.Settings(), {key: {"title": "範例寺", "lat": 1.0, "lon": 2.0,
                                                       "place": "X"}})
    assert items[0]["title"] == "範例寺" and items[0]["loc_source"] == "manual"
    assert items[0]["place"] == "X"


def test_reset_clock_is_suspect_until_time_is_set(tmp_path):
    # three clips stamped with the same start time = the camera clock was reset
    for n in (797, 798, 799):
        make_mp4(tmp_path / f"GX010{n}.MP4", datetime(2022, 5, 24, 17, 10, 32), 4.0, [])
    make_mp4(tmp_path / "GX010827.MP4", CAM, 4.0, [])
    recs = scan(str(tmp_path))
    items = {i["files"][0]: i for i in build_plan(recs, config.Settings(), {})}
    assert [items[f"GX010{n}.MP4"]["time_suspect"] for n in (797, 798, 799)] == [True] * 3
    assert not items["GX010827.MP4"]["time_suspect"]
    assert not any(i["include"] for i in items.values())  # opt-in: nothing ticked by default

    key = items["GX010798.MP4"]["id"]
    items = {i["files"][0]: i for i in build_plan(
        recs, config.Settings(), {key: {"local_time": "2025-04-03T21:05"}})}
    fixed = items["GX010798.MP4"]
    assert fixed["title"] == "2025-04-03 21:05 JST" and fixed["time_source"] == "manual"
    assert fixed["recording_date"] == "2025-04-03T00:00:00Z" and fixed["utc"].startswith("2025-04-03T12:05")
    assert not fixed["time_suspect"]


def _gopro_clip(path, utc_start, seconds, camera_offset_h):
    """Like a real HERO: mvhd in UTC, file time = camera's local clock at the end."""
    make_mp4(path, utc_start, seconds, [])
    end_local = utc_start + timedelta(hours=camera_offset_h, seconds=seconds)
    ts = end_local.timestamp()  # naive -> this PC's zone; fromtimestamp() reverses it
    os.utime(path, (ts, ts))


def test_each_recording_uses_the_zone_the_camera_was_in(tmp_path):
    _gopro_clip(tmp_path / "GX010827.MP4", datetime(2025, 4, 4, 8, 33, 21), 10, 8)   # Taiwan
    _gopro_clip(tmp_path / "GX010996.MP4", datetime(2025, 4, 7, 8, 51, 29), 10, 9)   # Japan
    recs = scan(str(tmp_path))
    items = {i["files"][0]: i for i in build_plan(recs, config.Settings(), {})}
    tw, jp = items["GX010827.MP4"], items["GX010996.MP4"]
    assert tw["title"] == "2025-04-04 16:33" and tw["tz"] == "Asia/Taipei" and tw["tz_label"] == ""
    assert jp["title"] == "2025-04-07 17:51 JST" and jp["tz"] == "Asia/Tokyo"
    assert tw["tz_source"] == jp["tz_source"] == "file"
    assert tw["recording_date"] == "2025-04-04T00:00:00Z"

    # the user can re-pick a zone; the title follows, the moment in time stays the same
    key = tw["id"]
    tw2 = build_plan(recs, config.Settings(), {key: {"tz": "Asia/Tokyo"}})[0]
    assert tw2["title"] == "2025-04-04 17:33 JST" and tw2["utc"] == tw["utc"]
    # a typed time is read in the recording's zone
    tw3 = build_plan(recs, config.Settings(), {key: {"local_time": "2025-04-04T09:00"}})[0]
    assert tw3["title"] == "2025-04-04 09:00" and tw3["utc"].startswith("2025-04-04T01:00")


def test_edits_are_logged_marked_and_resettable(tmp_path, caplog):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010798.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    item = api.scan(str(tmp_path))["items"][0]
    assert item["edited"] == []
    with caplog.at_level("INFO"):
        res = api.edit([item["id"]], {"place": "範例車站", "local_time": "2025-04-04T09:00"})
    assert "修改 GX010798.MP4：地點名稱 → 範例車站，拍攝時間 → 2025-04-04T09:00" in caplog.text
    assert res["items"][0]["edited"] == ["local_time", "place"]

    api.edit([item["id"]], {"include": True})
    res = api.reset_edits([item["id"]])
    it = res["items"][0]
    assert it["edited"] == [] and it["place"] == "" and it["time_source"] != "manual"
    assert it["include"] is True  # the tick is not an "edit" to undo
    assert Api("unused.log").scan(str(tmp_path))["items"][0]["edited"] == []  # and it's saved


def test_title_edit_persists_across_restarts_but_ticks_do_not(tmp_path):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    item = api.scan(str(tmp_path))["items"][0]
    assert item["title"] == "2026-03-15 14:32 JST"  # default, ready to be edited
    api.edit([item["id"]], {"title": "2026-03-15 14:32 JST 範例標題 (測試)", "include": True})

    item = Api("unused.log").scan(str(tmp_path))["items"][0]  # fresh app start
    assert item["title"] == "2026-03-15 14:32 JST 範例標題 (測試)" and item["title_locked"]
    assert item["include"] is False  # ticks are not remembered: a restart never queues uploads

    api2 = Api("unused.log")
    api2.scan(str(tmp_path))
    api2.edit([item["id"]], {"title": None})
    assert Api("unused.log").scan(str(tmp_path))["items"][0]["title"] == "2026-03-15 14:32 JST"


def test_title_date_mismatch_is_flagged(tmp_path):
    make_mp4(tmp_path / "GX010797.MP4", CAM, 4.0, [])
    recs = scan(str(tmp_path))
    key = recs[0].key
    plan = lambda ov: build_plan(recs, config.Settings(), {key: ov})[0]  # noqa: E731
    assert plan({"title": "2025-04-25 範例車站 東口"})["title_date_mismatch"]
    assert not plan({"title": "2026-03-15 範例車站"})["title_date_mismatch"]
    assert not plan({"title": "2026年3月15日 範例車站"})["title_date_mismatch"]
    assert not plan({"title": "範例車站（沒有日期）"})["title_date_mismatch"]
    assert not plan({})["title_date_mismatch"]  # automatic titles always match


def test_video_body_sends_languages():
    from gopro_uploader.youtube import video_body
    b = video_body("t", "d", "19", "unlisted", "2025-04-04T00:00:00Z",
                   title_language="zh-TW", audio_language="zh-TW")
    assert b["snippet"]["defaultLanguage"] == "zh-TW"
    assert b["snippet"]["defaultAudioLanguage"] == "zh-TW"
    assert config.Settings().title_language == config.Settings().audio_language == "zh-TW"


def test_upload_body_sends_no_location():
    # YouTube ignores API-written locations (deprecated 2017, "Won't Fix"); the user sets it in Studio
    from gopro_uploader import youtube
    b = youtube.video_body("t", "d", "19", "unlisted", "2025-04-04T00:00:00Z")
    assert b["recordingDetails"] == {"recordingDate": "2025-04-04T00:00:00Z"}
    assert not hasattr(youtube, "set_location")


def test_uploaded_recordings_can_be_edited_and_pushed_later(tmp_path):
    # upload first, fix titles afterwards: edits are kept and marked for YouTube
    from gopro_uploader import youtube
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010797.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    api._emit = lambda *a, **k: None
    item = api.scan(str(tmp_path))["items"][0]
    api._store.update(item, status="uploaded", video_ids=["B8"],
                      yt={"B8": {"title": "2026-03-15 14:32 JST 在 Studio 打的字", "description": "GX010797.MP4",
                                 "privacy": "unlisted", "recording_date": "2026-03-15", "place": "",
                                 "lat": None, "lon": None, "title_language": "zh-TW", "audio_language": "zh-TW"}},
                      yt_checked="2025-04-27T10:00")
    it = api._replan()["items"][0]
    assert it["title"] == "2026-03-15 14:32 JST 在 Studio 打的字" and it["yt_pending"] == []

    # a new time rewrites only the leading time of YouTube's title, and marks it pending
    it = api.edit([item["id"]], {"local_time": "2026-03-15T15:00"})["items"][0]
    assert it["title"] == "2026-03-15 15:00 JST 在 Studio 打的字" and it["yt_pending"] == ["title"]
    note = "GX010797.MP4\n連 APP 更新時區"
    it = api.edit([item["id"]], {"description": note})["items"][0]
    assert it["yt_pending"] == ["description", "title"]

    sent = []
    upd = youtube.update_snippet
    youtube.update_snippet = lambda yt, vid, title=None, description=None: sent.append((vid, title, description)) or {}
    fetch = youtube.fetch_videos
    youtube.fetch_videos = lambda yt, ids: {}
    try:
        api._yt = object()
        api.push_to_youtube()
    finally:
        youtube.update_snippet, youtube.fetch_videos = upd, fetch
    assert sent == [("B8", "2026-03-15 15:00 JST 在 Studio 打的字", note)]
    assert (api._store.lookup(item) or {}).get("yt_dirty") == []


def test_uploaded_rows_show_youtube_data_and_deleted_videos_can_be_reuploaded(tmp_path):
    from gopro_uploader import youtube
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010797.MP4", CAM, 4.0, [])
    make_mp4(tmp_path / "GX010798.MP4", CAM.replace(minute=50), 4.0, [])
    api = Api("unused.log")
    a, b = api.scan(str(tmp_path))["items"]
    api.edit([a["id"]], {"title": "2025-04-25 範例車站 東口"})  # the typo, uploaded
    api._store.update(a, status="uploaded", video_ids=["VIDEO_ID_01"])
    api._store.update(b, status="uploaded", video_ids=["gone"])

    asked = []

    def fake_fetch(yt, ids):
        asked.append(sorted(ids))
        return {"VIDEO_ID_01": {"title": "2025-04-04 範例車站 東口", "privacy": "unlisted",
                                "recording_date": "2025-04-04", "place": "車站大廳",
                                "lat": 12.3456, "lon": 22.6543,
                                "title_language": "zh-TW", "audio_language": "zh-TW"}}

    youtube_fetch, youtube.fetch_videos = youtube.fetch_videos, fake_fetch
    try:
        api._yt = object()  # "signed in"
        items = {i["files"][0]: i for i in api.scan(str(tmp_path))["items"]}
    finally:
        youtube.fetch_videos = youtube_fetch
    assert asked == [["VIDEO_ID_01", "gone"]]
    a, b = items["GX010797.MP4"], items["GX010798.MP4"]
    assert a["status"] == "uploaded" and a["title"] == "2025-04-04 範例車站 東口"
    assert a["yt"][0]["place"] == "車站大廳"
    assert a["yt_place"] == "車站大廳" and a["yt_has_location"]  # the row shows YouTube's place
    assert b["status"] == "missing" and b["yt"] == [{"id": "gone", "missing": True}]
    assert not api._locked(b["id"])  # can be edited and ticked again

    # restart with an expired sign-in: the copy read from YouTube was saved, so it is still shown
    offline = Api("unused.log")
    items = {i["files"][0]: i for i in offline.scan(str(tmp_path))["items"]}
    a = items["GX010797.MP4"]
    assert a["title"] == "2025-04-04 範例車站 東口" and a["yt_checked"]
    assert items["GX010798.MP4"]["status"] == "missing"


def test_not_signed_in_keeps_the_local_view(tmp_path):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010797.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    a = api.scan(str(tmp_path))["items"][0]
    api._store.update(a, status="uploaded", video_ids=["VIDEO_ID_01"])
    a = api.scan(str(tmp_path))["items"][0]  # api._yt is None
    assert a["status"] == "uploaded" and "yt" not in a


def test_places_set_by_search_become_recent_quick_picks(tmp_path):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    key = api.scan(str(tmp_path))["items"][0]["id"]
    api.edit([key], {"place": "車站大廳", "lat": 12.3456, "lon": 22.6543})
    api.edit([key], {"place": "範例城", "lat": 11.2345, "lon": 21.5432})
    res = api.edit([key], {"place": "車站大廳", "lat": 12.3456, "lon": 22.6543})
    assert [p["place"] for p in res["recent_places"]] == ["車站大廳", "範例城"]  # newest first, no dupes
    api.edit([key], {"place": "只打名字"})  # no coordinates: not a quick pick
    assert [p["place"] for p in config.Settings.load().recent_places] == ["車站大廳", "範例城"]


def test_retime_title_keeps_the_users_words():
    from gopro_uploader.planner import retime_title
    t = datetime(2025, 4, 4, 13, 10)
    assert retime_title("2022-05-25 走到出口", t) == "2025-04-04 走到出口"
    assert retime_title("2022-05-25 01:10 走到出口", t) == "2025-04-04 13:10 走到出口"
    assert retime_title("2022-05-25 01:10 JST 走到出口", t) == "2025-04-04 13:10 JST 走到出口"
    assert retime_title("2022/05/25 走到出口", t) == "2025/04/04 走到出口"
    assert retime_title("2022年05月25日 走到出口", t) == "2025年04月04日 走到出口"
    assert retime_title("走到出口", t) == "走到出口"  # no leading date: untouched


def test_changing_the_time_updates_a_typed_title_instead_of_warning(tmp_path):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010798.MP4", datetime(2022, 5, 24, 17, 10, 32), 4.0, [])
    api = Api("unused.log")
    key = api.scan(str(tmp_path))["items"][0]["id"]
    api.edit([key], {"title": "2022-05-25 走到出口"})
    it = api.edit([key], {"local_time": "2025-04-04T13:10"})["items"][0]
    assert it["title"] == "2025-04-04 走到出口" and not it["title_date_mismatch"]
    # and it is saved like any other edit
    assert Api("unused.log").scan(str(tmp_path))["items"][0]["title"] == "2025-04-04 走到出口"

    # a date typed wrong by hand can be fixed without losing the words
    api.edit([key], {"title": "2025-04-25 走到出口"})
    assert api._items[0]["title_date_mismatch"]
    it = api.fix_title_dates([key])["items"][0]
    assert it["title"] == "2025-04-04 走到出口"


class _FakePlaylists:
    """Minimal stand-in for yt.playlists(): one page, insert echoes back."""

    def __init__(self, items):
        self.items, self.inserted = items, []

    def list(self, **kw):
        return _Req({"items": self.items})

    def list_next(self, req, resp):
        return None

    def insert(self, part, body):
        self.inserted.append(body)
        return _Req({"id": "PLnew", "snippet": {"title": body["snippet"]["title"], "publishedAt": "2025-04-25T00:00:00Z"},
                     "status": body["status"]})


class _Req:
    def __init__(self, result):
        self.result = result

    def execute(self, num_retries=0):
        return self.result


def test_playlists_are_listed_newest_first_and_created_explicitly():
    from gopro_uploader import youtube
    pl = _FakePlaylists([
        {"id": "PL22", "snippet": {"title": "2022 日本", "publishedAt": "2022-05-01T00:00:00Z"},
         "status": {"privacyStatus": "unlisted"}, "contentDetails": {"itemCount": 40}},
        {"id": "PL25", "snippet": {"title": "2025 日本", "publishedAt": "2025-04-20T00:00:00Z"},
         "status": {"privacyStatus": "unlisted"}, "contentDetails": {"itemCount": 2}},
    ])
    yt = type("YT", (), {"playlists": lambda self: pl})()
    got = youtube.list_playlists(yt)
    assert [(p["title"], p["count"]) for p in got] == [("2025 日本", 2), ("2022 日本", 40)]
    p = youtube.create_playlist(yt, "2026 韓國", "private")
    assert p["id"] == "PLnew" and pl.inserted[0]["status"]["privacyStatus"] == "private"
    assert not hasattr(youtube, "find_or_create_playlist")  # no silent creation from a typed name


def _upload_api(tmp_path, monkeypatch, n, uploader):
    """An Api with n recordings ticked, signed in to a fake YouTube whose upload is `uploader`."""
    from gopro_uploader import youtube
    from gopro_uploader.app import Api
    for k in range(n):
        make_mp4(tmp_path / f"GX01{k + 1:04d}.MP4", CAM + timedelta(minutes=10 * k), 4.0, [])
    api = Api("unused.log")
    events = []
    api._emit = lambda kind, **d: events.append((kind, d))
    ids = [i["id"] for i in api.scan(str(tmp_path))["items"]]
    api.edit(ids, {"include": True})
    api.choose_playlist("PL25", "2025 日本")
    added = []
    monkeypatch.setattr(api, "_signed_in", lambda: "yt")
    monkeypatch.setattr(youtube, "service_copy", lambda yt: yt)
    monkeypatch.setattr(youtube, "add_to_playlist", lambda yt, pl, vid: added.append((pl, vid)))
    monkeypatch.setattr(youtube, "upload", uploader)
    return api, events, added


def test_parallel_uploads_keep_file_order_in_the_playlist(tmp_path, monkeypatch):
    import time as _time

    finished = []

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        k = int(os.path.basename(path)[4:8])
        on_session(f"uri{k}")
        _time.sleep(0.05 * (5 - k))  # the first file is the slowest: they finish in reverse
        progress(1, 2)
        before_last()  # …but each holds its last chunk until the one before has finished
        finished.append(k)
        return f"V{k}"

    api, events, added = _upload_api(tmp_path, monkeypatch, 4, upload)
    api._settings.parallel_uploads = 3
    api._upload_all()
    assert finished == [1, 2, 3, 4]  # finished, so listed in Studio, in file order
    assert [v for _, v in added] == ["V1", "V2", "V3", "V4"]  # playlist too
    assert all(p == "PL25" for p, _ in added)
    assert events[-1][0] == "done" and "完成 4 / 4" in events[-1][1]["msg"]
    assert not any(r.get("playlist_add") or r.get("resume") for r in api._store.data.values())
    assert any("平均" in d.get("msg", "") and "MB/s" in d["msg"] for k, d in events if k == "log")


def test_a_failure_pauses_the_batch_to_keep_the_order(tmp_path, monkeypatch):
    import threading
    from gopro_uploader import youtube
    first_failed = threading.Event()

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        k = int(os.path.basename(path)[4:8])
        on_session(f"uri{k}")
        if k == 1:
            raise RuntimeError("重試太多次仍失敗")
        first_failed.wait(2)
        while not cancel():  # the later ones are under way when the first one fails…
            pass
        raise youtube.Cancelled()  # …and are stopped, to go on from there next time

    api, events, added = _upload_api(tmp_path, monkeypatch, 3, upload)
    api._settings.parallel_uploads = 3
    monkeypatch.setattr(api, "_set_status", (lambda orig: lambda item, status, **kw: (
        first_failed.set() if status == "error" else None, orig(item, status, **kw)))(api._set_status))
    api._upload_all()
    status = [api._store.lookup(i)["status"] for i in api._items]
    assert status == ["error", "pending", "pending"] and added == []
    assert all(api._store.lookup(i).get("resume") for i in api._items[1:])
    assert "暫停" in events[-1][1]["msg"]


def test_a_failure_lets_the_ones_before_it_finish(tmp_path, monkeypatch):
    import threading
    from gopro_uploader import youtube
    second_failed = threading.Event()

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        k = int(os.path.basename(path)[4:8])
        on_session(f"uri{k}")
        progress(1, 2)
        if k == 2:
            raise RuntimeError("合併失敗")
        second_failed.wait(2)  # all of them are under way when the second one fails
        before_last()          # the third must not be let through by the failed second
        if cancel():
            raise youtube.Cancelled()
        return f"V{k}"

    api, events, added = _upload_api(tmp_path, monkeypatch, 4, upload)
    api._settings.parallel_uploads = 4
    monkeypatch.setattr(api, "_set_status", (lambda orig: lambda item, status, **kw: (
        second_failed.set() if status == "error" else None, orig(item, status, **kw)))(api._set_status))
    api._upload_all()
    status = [api._store.lookup(i)["status"] for i in api._items]
    assert status == ["uploaded", "error", "pending", "pending"]
    assert [v for _, v in added] == ["V1"]
    assert all(api._store.lookup(i).get("resume") for i in api._items[2:])
    assert not api._cancel


def test_quota_stop_waits_for_the_reset_and_goes_on(tmp_path, monkeypatch):
    from gopro_uploader import app as app_mod
    from gopro_uploader import youtube
    calls = []

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        k = int(os.path.basename(path)[4:8])
        calls.append(k)
        if k == 2 and calls.count(2) == 1:
            raise youtube.QuotaExceeded("uploadLimitExceeded")
        return f"V{k}"

    monkeypatch.setattr(app_mod, "quota_reset", lambda: datetime.now() + timedelta(seconds=0.2))
    monkeypatch.setattr(app_mod, "QUOTA_POLL", 0.05)
    api, events, added = _upload_api(tmp_path, monkeypatch, 3, upload)
    api._settings.parallel_uploads = 1
    api._upload_all()
    assert calls == [1, 2, 2, 3]  # the stopped one goes first after the reset
    assert [v for _, v in added] == ["V1", "V2", "V3"]
    assert [d["until"] is None for k, d in events if k == "quota_wait"] == [False, True]
    assert sum("自動繼續" in d.get("msg", "") for _, d in events) == 1
    assert events[-1][0] == "done" and "完成 3 / 3" in events[-1][1]["msg"]


def test_quota_stop_tries_every_hour_before_the_reset(tmp_path, monkeypatch):
    from gopro_uploader import app as app_mod
    from gopro_uploader import youtube
    calls = []

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        k = int(os.path.basename(path)[4:8])
        calls.append(k)
        if k == 1 and calls.count(1) <= 2:  # still blocked at the first hourly try
            raise youtube.QuotaExceeded("rateLimitExceeded")
        return f"V{k}"

    reset = datetime.now() + timedelta(hours=10)
    monkeypatch.setattr(app_mod, "quota_reset", lambda: reset)
    monkeypatch.setattr(app_mod, "QUOTA_PROBE", timedelta(seconds=0.1))
    monkeypatch.setattr(app_mod, "QUOTA_POLL", 0.02)
    api, events, added = _upload_api(tmp_path, monkeypatch, 2, upload)
    api._settings.parallel_uploads = 1
    api._upload_all()
    assert calls == [1, 1, 1, 2]  # the third try (second hour) gets through, long before the reset
    assert sum("自動繼續" in d.get("msg", "") for _, d in events) == 1  # said once, not every hour
    assert sum("試試看" in d.get("msg", "") for _, d in events) == 2
    assert events[-1][0] == "done" and "完成 2 / 2" in events[-1][1]["msg"]


def test_ones_behind_a_quota_refusal_do_not_try(tmp_path, monkeypatch):
    from gopro_uploader import app as app_mod
    from gopro_uploader import youtube
    calls = []

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        k = int(os.path.basename(path)[4:8])
        calls.append(k)
        if k == 2 and calls.count(2) == 1:
            raise youtube.QuotaExceeded("rateLimitExceeded")
        on_session(f"https://upload.example/{k}")
        return f"V{k}"

    monkeypatch.setattr(app_mod, "quota_reset", lambda: datetime.now() + timedelta(seconds=0.2))
    monkeypatch.setattr(app_mod, "QUOTA_POLL", 0.05)
    api, events, added = _upload_api(tmp_path, monkeypatch, 4, upload)
    api._settings.parallel_uploads = 4
    api._upload_all()
    # 3 and 4 were already queued behind 2, but must not get in ahead of it (Studio order)
    assert calls == [1, 2, 2, 3, 4]
    assert [v for _, v in added] == ["V1", "V2", "V3", "V4"]


def test_stop_while_waiting_for_the_quota(tmp_path, monkeypatch):
    from gopro_uploader import app as app_mod
    from gopro_uploader import youtube

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        raise youtube.QuotaExceeded("uploadLimitExceeded")

    monkeypatch.setattr(app_mod, "quota_reset", lambda: datetime.now() + timedelta(hours=5))
    monkeypatch.setattr(app_mod, "QUOTA_POLL", 0.05)
    api, events, added = _upload_api(tmp_path, monkeypatch, 2, upload)
    api._emit = lambda kind, **d: (events.append((kind, d)), kind == "quota_wait" and api.cancel())
    api._upload_all()
    assert [(api._store.lookup(i) or {}).get("status", "pending") for i in api._items] == ["pending", "pending"]
    assert "配額用完" in events[-1][1]["msg"]


def test_quota_reset_is_after_pacific_midnight():
    from gopro_uploader.app import quota_reset
    tw = ZoneInfo("Asia/Taipei")
    now = datetime(2025, 4, 28, 1, 28, tzinfo=tw).astimezone().replace(tzinfo=None)
    at = quota_reset(now).replace(tzinfo=None).astimezone(tw)
    assert (at.hour, at.minute) == (15, 5)  # PDT: midnight = 15:00 in Taiwan
    just_after = datetime(2025, 4, 28, 15, 10, tzinfo=tw).astimezone().replace(tzinfo=None)
    assert quota_reset(just_after) - just_after == timedelta(minutes=30)  # reset didn't help: retry soon


def test_stopped_upload_goes_on_from_where_it_was(tmp_path, monkeypatch):
    from gopro_uploader import youtube
    calls = []

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        calls.append(resume_uri)
        if len(calls) == 1:
            on_session("https://upload.example/session1")  # session open, part sent…
            progress(41, 100)
            raise youtube.Cancelled()                        # …then the stop button
        return "V1"

    api, events, added = _upload_api(tmp_path, monkeypatch, 1, upload)
    api._upload_all()
    item = api._items[0]
    assert item["status"] == "pending" and api._store.lookup(item)["resume"]["uri"]

    api.scan(str(tmp_path))  # a restart: same file, same title
    r = api._items[0]["resume"]  # shown as 已中斷 41%
    assert round(r["pct"]) == 41 and not r["changed"] and not r["expired"] and r["until"]
    api.edit([api._items[0]["id"]], {"include": True})
    api._upload_all()
    assert calls == [None, "https://upload.example/session1"]
    assert added == [("PL25", "V1")] and not api._store.lookup(api._items[0]).get("resume")


def test_first_upload_cut_off_mid_way_still_plans(tmp_path, monkeypatch):
    # killed (or edited) during a first upload: state.json has the resume info but no status
    api, events, added = _upload_api(tmp_path, monkeypatch, 1, lambda *a, **k: "V1")
    api._store.update(api._items[0], resume={"uri": "https://upload.example/s", "sig": "x", "part": 1, "parts": 1})
    assert "status" not in api._store.lookup(api._items[0])
    api.scan(str(tmp_path))
    assert api._items[0]["status"] == "pending"


def test_a_resumed_upload_lets_the_next_one_start(tmp_path, monkeypatch):
    from gopro_uploader import youtube
    second = threading.Event()

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        if path.endswith("GX010002.MP4"):
            second.set()
            return "V2"
        if not resume_uri:
            on_session("https://upload.example/session1")
            raise youtube.Cancelled()
        # resumed: no new session is opened, yet the next recording must not wait for this one
        assert second.wait(5), "the next upload waited for the resumed one to finish"
        return "V1"

    api, events, added = _upload_api(tmp_path, monkeypatch, 2, upload)
    api._settings.parallel_uploads = 2
    api.edit([api._items[1]["id"]], {"include": False})
    api._upload_all()  # the first one stops part-way
    api.edit([i["id"] for i in api._items], {"include": True})
    api._upload_all()
    assert [i["status"] for i in api._items] == ["uploaded", "uploaded"]


def test_changed_title_starts_a_stopped_upload_over(tmp_path, monkeypatch):
    from gopro_uploader import youtube
    calls = []

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        calls.append(resume_uri)
        if len(calls) == 1:
            on_session("https://upload.example/session1")
            raise youtube.Cancelled()
        return "V1"

    api, events, added = _upload_api(tmp_path, monkeypatch, 1, upload)
    api._upload_all()
    api.edit([api._items[0]["id"]], {"title": "範例寺", "include": True})
    assert api._items[0]["resume"]["changed"]  # the row says it will start over
    api._upload_all()
    assert calls == [None, None]  # the session carries the old title, so it is not reused


def test_closing_the_window_stops_uploads(tmp_path, monkeypatch):
    from gopro_uploader import youtube
    sent = threading.Event()

    def upload(yt, path, body, progress=None, cancel=None, resume_uri=None, on_session=None, before_last=None):
        on_session("https://upload.example/session1")
        sent.set()
        while not cancel():
            time.sleep(0.01)
        raise youtube.Cancelled()

    api, events, added = _upload_api(tmp_path, monkeypatch, 1, upload)
    assert api.close_question() is None  # idle: just close
    api.start_upload()
    assert sent.wait(5)
    assert "正在上傳 1 支" in api.close_question()
    assert api.shut_down(timeout=5)  # stopped, place kept for next time
    assert not api._worker.is_alive() and api._store.lookup(api._items[0])["resume"]["uri"]


def test_playlist_adds_left_by_a_crash_are_done_first(tmp_path, monkeypatch):
    def upload(*a, **k):
        raise AssertionError("nothing to upload")

    api, events, added = _upload_api(tmp_path, monkeypatch, 1, upload)
    item = api._items[0]
    api._store.update(item, status="uploaded", video_ids=["V9"], playlist_add={"playlist": "PL25", "ids": ["V9"]})
    api._items[0]["include"], api._items[0]["status"] = False, "uploaded"
    api._items.append(dict(item, id="other", status="pending", include=True))  # something to upload
    monkeypatch.setattr(api, "_upload_item", lambda *a, **k: None)
    api._upload_all()
    assert added == [("PL25", "V9")] and "playlist_add" not in api._store.lookup(item)


def test_upload_needs_a_chosen_playlist(tmp_path):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    events = []
    api._emit = lambda kind, **d: events.append((kind, d))
    key = api.scan(str(tmp_path))["items"][0]["id"]
    api.edit([key], {"include": True})
    api._upload_all()  # nothing chosen: stops before signing in or uploading
    assert events[-1][0] == "done" and "播放清單" in events[-1][1]["msg"]

    s = api.choose_playlist("PL25", "2025 日本")
    assert s["playlist_id"] == "PL25" and not s["no_playlist"]
    s = api.choose_playlist(None, None)
    assert s["no_playlist"] and config.Settings.load().no_playlist


def test_export_logs_never_includes_sign_in_secrets(tmp_path):
    import zipfile
    from gopro_uploader import store
    for name, body in [("token.json", "SECRET-TOKEN"), ("client_secret.json", "SECRET-CLIENT"),
                       ("settings.json", "{}"), ("state.json", "{}"), ("upload_history.csv", "a,b")]:
        with open(config.path(name), "w", encoding="utf-8") as f:
            f.write(body)
    for n in range(12):
        with open(config.path("logs", f"20250425-1200{n:02d}.log"), "w", encoding="utf-8") as f:
            f.write(f"log {n}")
    dest = tmp_path / "export.zip"
    names = store.export_logs(str(dest))
    with zipfile.ZipFile(dest) as z:
        listed = z.namelist()
        blob = b"".join(z.read(n) for n in listed)
    assert set(names) <= set(listed) and "README.txt" in listed
    assert "settings.json" in listed and "state.json" in listed and "upload_history.csv" in listed
    assert sum(n.startswith("logs/") for n in listed) == 10  # newest 10 only
    assert "logs/20250425-120011.log" in listed and "logs/20250425-120000.log" not in listed
    assert not any("token" in n or "client_secret" in n for n in listed)
    assert b"SECRET" not in blob


def test_open_path_only_opens_known_places(tmp_path):
    from gopro_uploader.app import Api
    assert Api("unused.log").open_path(str(tmp_path)) is False


def test_dead_sign_in_is_reported_and_forgotten(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    from gopro_uploader import youtube
    from gopro_uploader.app import Api

    with open(config.path("token.json"), "w") as f:
        f.write("{}")
    api = Api("unused.log")
    events = []
    api._emit = lambda kind, **d: events.append((kind, d))

    class Dead:  # a service whose every call fails like an expired refresh token
        def channels(self):
            return self

        def list(self, **kw):
            return self

        def execute(self, num_retries=0):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    api._yt = Dead()
    assert api.check_login() is None
    assert ("signed_out", {}) in events and api._yt is None
    assert not youtube.has_saved_login()  # dead token removed: next sign-in opens the browser
    n = len(events)
    api._signed_out()  # reported once, not on every call
    assert len(events) == n


def test_offline_is_not_signed_out(monkeypatch):
    import requests
    from gopro_uploader import youtube
    from gopro_uploader.app import Api
    api = Api("unused.log")
    events = []
    api._emit = lambda kind, **d: events.append((kind, d))

    class Offline:
        def channels(self):
            return self

        def list(self, **kw):
            return self

        def execute(self, num_retries=0):
            raise requests.ConnectionError("no network")

    api._yt = Offline()
    assert api.check_login() is None
    assert api._yt is not None and not any(k == "signed_out" for k, _ in events)


def test_refresh_after_upload_reads_youtube_even_if_items_went_stale(tmp_path):
    # GX010799 uploads; meanwhile GX010800 is edited (re-plan marks 799 "uploading");
    # the refresh after "done" must still read 799 from YouTube, and 799 is unticked.
    from gopro_uploader import youtube
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010799.MP4", CAM, 4.0, [])
    make_mp4(tmp_path / "GX010800.MP4", CAM.replace(minute=50), 4.0, [])
    api = Api("unused.log")
    api._emit = lambda *a, **k: None
    api._yt = object()
    a, b = api.scan(str(tmp_path))["items"]
    api.edit([a["id"]], {"include": True})
    api._uploading_ids.add(a["id"])
    api.edit([b["id"]], {"title": "往出口移動"})  # re-plan during the upload
    worker_item = next(i for i in api._items if i["id"] == a["id"])
    assert worker_item["status"] == "uploading"
    worker_item["video_ids"] = ["VIDEO_ID_02"]
    api._set_status(worker_item, "uploaded")
    api._uploading_ids.discard(a["id"])

    fetch, youtube.fetch_videos = youtube.fetch_videos, lambda yt, ids: {
        "VIDEO_ID_02": {"title": "2025-04-04 03:16", "description": "", "privacy": "unlisted",
                        "recording_date": "2025-04-04", "place": "", "lat": None, "lon": None,
                        "title_language": "zh-TW", "audio_language": "zh-TW"}}
    try:
        items = {i["files"][0]: i for i in api.refresh_youtube()["items"]}
    finally:
        youtube.fetch_videos = fetch
    up = items["GX010799.MP4"]
    assert up["status"] == "uploaded" and up["yt"][0]["id"] == "VIDEO_ID_02"
    assert up["include"] is False


def test_only_scanned_files_can_be_opened_or_revealed(tmp_path, monkeypatch):
    import subprocess
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, [])
    make_mp4(tmp_path / "GX020001.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    key = api.scan(str(tmp_path))["items"][0]["id"]
    ran = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: ran.append(args))
    assert api.reveal_file(key, 1) is True
    assert ran == [["explorer", f"/select,{os.path.normpath(str(tmp_path / 'GX020001.MP4'))}"]]
    assert api.reveal_file(key, 5) is False and api.reveal_file("not-a-key") is False
    assert api.open_original(str(tmp_path / "GX010001.MP4")) is False  # a path is not an id


def test_clock_reset_clips_get_the_neighbours_date_and_a_date_only_title(tmp_path):
    reset = datetime(2022, 5, 24, 17, 10, 32)
    for n in (797, 798, 799):
        make_mp4(tmp_path / f"GX010{n}.MP4", reset, 4.0, [])
    _gopro_clip(tmp_path / "GX010827.MP4", datetime(2025, 4, 4, 8, 33, 21), 10, 8)   # good clock
    recs = scan(str(tmp_path))
    items = {i["files"][0]: i for i in build_plan(recs, config.Settings(), {})}
    for n in (797, 798, 799):
        it = items[f"GX010{n}.MP4"]
        assert it["time_suspect"] and it["date_guess"] == "2025-04-04" and not it["date_uncertain"]
        assert not it["time_known"] and it["recording_date"] == "2025-04-04T00:00:00Z"
    assert items["GX010798.MP4"]["title"].startswith("2025-04-04") and ":" not in items["GX010798.MP4"]["title"]
    assert items["GX010827.MP4"]["title"] == "2025-04-04 16:33"

    # a typed title on a guessed day keeps its words, the date comes from the guess
    key = items["GX010798.MP4"]["id"]
    it = build_plan(recs, config.Settings(), {key: {"title": "2025-04-04 走到出口"}})
    assert {i["files"][0]: i for i in it}["GX010798.MP4"]["title"] == "2025-04-04 走到出口"


def test_guess_across_midnight_is_flagged(tmp_path):
    _gopro_clip(tmp_path / "GX010100.MP4", datetime(2025, 4, 5, 14, 50), 10, 9)  # 23:50 JST 4/5
    make_mp4(tmp_path / "GX010101.MP4", datetime(2023, 1, 1), 4.0, [])
    make_mp4(tmp_path / "GX010102.MP4", datetime(2023, 1, 1), 4.0, [])
    _gopro_clip(tmp_path / "GX010103.MP4", datetime(2025, 4, 5, 15, 20), 10, 9)  # 00:20 JST 4/6
    items = {i["files"][0]: i for i in build_plan(scan(str(tmp_path)), config.Settings(), {})}
    mid = items["GX010101.MP4"]
    assert mid["date_guess"] == "2025-04-06" and mid["date_uncertain"]


def test_date_only_time_and_batch_dates(tmp_path):
    from gopro_uploader.app import Api
    from gopro_uploader.planner import date_part, retime_title
    assert date_part("%Y-%m-%d %H:%M") == "%Y-%m-%d" and date_part("%Y年%m月%d日 %H:%M") == "%Y年%m月%d日"
    assert retime_title("2022-05-25 01:10 走到出口", datetime(2025, 4, 4), with_time=False) == "2025-04-04 走到出口"

    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, [])
    make_mp4(tmp_path / "GX010002.MP4", CAM.replace(minute=50), 4.0, [])
    api = Api("unused.log")
    a, b = api.scan(str(tmp_path))["items"]
    it = api.edit([a["id"]], {"local_time": "2025-04-04"})["items"][0]   # date only
    assert not it["time_known"] and it["title"] == "2025-04-04" and it["recording_date"] == "2025-04-04T00:00:00Z"
    it = api.edit([a["id"]], {"local_time": "2025-04-04T15:16"})["items"][0]
    assert it["time_known"] and it["title"] == "2025-04-04 15:16 JST"

    items = {i["id"]: i for i in api.set_dates([a["id"], b["id"]], "2025-04-07")["items"]}
    assert items[a["id"]]["local"].startswith("2025-04-07 15:16")   # kept its time
    assert items[b["id"]]["local"].startswith("2025-04-07 14:50")


def test_clock_seen_in_the_video_sets_the_start_time(tmp_path):
    from gopro_uploader.app import Api
    reset = datetime(2022, 5, 24, 17, 10, 32)  # a reset clock, +8 file times
    _gopro_clip(tmp_path / "GX010809.MP4", reset, 400, 8)
    _gopro_clip(tmp_path / "GX010810.MP4", reset, 4, 8)
    _gopro_clip(tmp_path / "GX010827.MP4", datetime(2025, 4, 4, 8, 33, 21), 10, 8)
    api = Api("unused.log")
    items = {i["files"][0]: i for i in api.scan(str(tmp_path))["items"]}
    key = items["GX010809.MP4"]["id"]
    srcs = api.preview_sources(key)
    assert srcs[0]["start"] == 0
    # a clock shows 下午 3:54 at 2:48 into GX010809
    it = {i["files"][0]: i for i in api.time_from_clock(key, 168.0, "15:54")["items"]}
    assert it["GX010809.MP4"]["title"] == "2025-04-04 15:51" and it["GX010809.MP4"]["time_known"]
    assert it["GX010810.MP4"]["title"].startswith("2025-04-04") and not it["GX010810.MP4"]["time_known"]
    # just after midnight on the clock -> the recording started the day before
    it = {i["files"][0]: i for i in api.time_from_clock(key, 300.0, "00:02")["items"]}
    assert it["GX010809.MP4"]["local"].startswith("2025-04-03 23:57")


def test_vertical_short_clips_are_flagged(tmp_path):
    make_mp4(tmp_path / "GX010797.MP4", CAM, 24.0, [], video=(3840, 2160, 90))    # camera held sideways
    make_mp4(tmp_path / "GX010799.MP4", CAM, 36.0, [], video=(3840, 2160, 0))
    make_mp4(tmp_path / "GX010800.MP4", CAM, 400.0, [], video=(3840, 2160, 270))  # vertical but > 3 min
    items = {i["files"][0]: i for i in build_plan(scan(str(tmp_path)), config.Settings(), {})}
    assert items["GX010797.MP4"]["vertical"] and items["GX010797.MP4"]["short"]
    assert not items["GX010799.MP4"]["vertical"] and not items["GX010799.MP4"]["short"]
    assert items["GX010800.MP4"]["vertical"] and not items["GX010800.MP4"]["short"]


def test_old_cache_entries_get_orientation_without_reparsing(tmp_path, count_parses):
    import json
    make_mp4(tmp_path / "GX010797.MP4", CAM, 24.0, [], video=(3840, 2160, 90))
    scan(str(tmp_path))
    with open(config.path("scan_cache.json"), encoding="utf-8") as f:
        data = json.load(f)
    for e in data["entries"].values():
        e["info"].pop("dims")  # like a cache written before orientation existed
    with open(config.path("scan_cache.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)
    count_parses.clear()
    recs = scan(str(tmp_path))
    assert count_parses == [] and recs[0].clips[0].info.vertical  # header read only


def test_photos_explain_number_gaps(tmp_path):
    from gopro_uploader.app import Api
    from gopro_uploader.scanner import gopro_number, list_photos
    make_mp4(tmp_path / "GX010804.MP4", CAM, 4.0, [])
    make_mp4(tmp_path / "GX010807.MP4", CAM.replace(minute=40), 4.0, [])
    for n in ("GOPR0805.JPG", "G0010806.JPG"):
        (tmp_path / n).write_bytes(b"jpg")
    assert [p["number"] for p in list_photos(str(tmp_path))] == [805, 806]
    assert gopro_number("GX020715.MP4") == 715 and gopro_number("notes.jpg") is None
    res = Api("unused.log").scan(str(tmp_path))
    assert [i["number"] for i in res["items"]] == [804, 807] and len(res["photos"]) == 2


def test_window_opens_where_it_was_if_that_screen_is_still_there():
    from gopro_uploader.app import window_geometry
    screens = [(0, 0, 1920, 1000)]
    g = window_geometry({}, screens)  # first run: centred, fits the screen
    assert (g["width"], g["height"]) == (1440, 900) and g["x"] == (1920 - 1440) // 2
    g = window_geometry({"x": 100, "y": 50, "width": 1200, "height": 800, "maximized": True}, screens)
    assert (g["x"], g["y"], g["width"], g["height"], g["maximized"]) == (100, 50, 1200, 800, True)
    g = window_geometry({"x": 3000, "y": 50, "width": 1200, "height": 800}, screens)  # unplugged monitor
    assert g["x"] == (1920 - 1200) // 2 and g["y"] == (1000 - 800) // 2
    g = window_geometry({}, [(0, 0, 1280, 720)])  # small screen: shrink to fit (but not under the minimum)
    assert g["width"] == 1280 and g["height"] == 720


def test_taskbar_is_harmless_without_a_window():
    from gopro_uploader import taskbar
    tb = taskbar.Taskbar("no window has this title 9c1e")
    tb.progress(0.5)
    tb.state(taskbar.ERROR)
    tb.flash()  # nothing to show it on: silently does nothing


def test_taskbar_never_drives_another_process_window():
    # the tests run while the real app is open: its button must not blink or show progress
    from gopro_uploader import app, taskbar
    assert taskbar.Taskbar(app.WINDOW_TITLE)._window() is None


def test_only_scanned_photos_can_be_opened(tmp_path, monkeypatch):
    import subprocess
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010804.MP4", CAM, 4.0, [])
    (tmp_path / "GOPR0805.JPG").write_bytes(b"jpg")
    (tmp_path / "secret.txt").write_text("x")
    api = Api("unused.log")
    api.scan(str(tmp_path))
    ran = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: ran.append(args))
    assert api.reveal_photo(str(tmp_path), "GOPR0805.JPG") is True
    assert ran == [["explorer", f"/select,{os.path.normpath(str(tmp_path / 'GOPR0805.JPG'))}"]]
    assert api.reveal_photo(str(tmp_path), "secret.txt") is False
    assert api.open_photo(str(tmp_path), r"..\secret.txt") is False


def test_tz_override_note_and_own_description(tmp_path):
    from gopro_uploader.app import Api
    # arrived abroad with the camera still on home time; the next day the zone changed
    _gopro_clip(tmp_path / "GX010827.MP4", datetime(2025, 4, 4, 8, 33, 21), 10, 8)
    _gopro_clip(tmp_path / "GX010888.MP4", datetime(2025, 4, 6, 0, 30), 10, 9)
    api = Api("unused.log")
    items = {i["files"][0]: i for i in api.scan(str(tmp_path))["items"]}
    assert items["GX010827.MP4"]["description"] == "GX010827.MP4"  # nothing overridden yet

    items = {i["files"][0]: i for i in api.edit([items["GX010827.MP4"]["id"]], {"tz": "Asia/Tokyo"})["items"]}
    a = items["GX010827.MP4"]
    assert a["title"] == "2025-04-04 17:33 JST"
    assert a["description"] == "GX010827.MP4\n相機時間為 UTC+8，標題已換算為日本時間（JST）"
    assert items["GX010888.MP4"]["tz_note"] == ""  # camera already on +9: no note

    b = items["GX010888.MP4"]
    note = "GX010888.MP4\n昨晚用 APP 連手機，更新了時區"
    api.edit([b["id"]], {"description": note})
    again = {i["files"][0]: i for i in Api("unused.log").scan(str(tmp_path))["items"]}
    assert again["GX010888.MP4"]["description"] == note and "description" in again["GX010888.MP4"]["edited"]


def test_daily_429_counts_as_quota_but_short_429_is_retried():
    import json

    import httplib2
    from googleapiclient.errors import HttpError

    from gopro_uploader import youtube

    def err(message):
        body = {"error": {"code": 429, "message": message,
                          "errors": [{"reason": "rateLimitExceeded", "message": message}]}}
        return HttpError(httplib2.Response({"status": 429}), json.dumps(body).encode())

    daily = ("Quota exceeded for quota metric 'Video Uploads' and limit 'Video Uploads per day' "
             "of service 'youtube.googleapis.com'")
    with pytest.raises(youtube.QuotaExceeded):
        youtube._raise_if_quota(err(daily))
    youtube._raise_if_quota(err("Too many requests"))  # not a quota stop
    assert 429 in youtube.RETRY_STATUS


def test_merged_file_goes_to_app_data_when_the_card_folder_is_read_only(tmp_path, monkeypatch):
    from gopro_uploader import merge
    a, b = tmp_path / "cam" / "GX010001.MP4", tmp_path / "cam" / "GX020001.MP4"
    other = tmp_path / "cam2" / "GX010001.MP4"
    assert merge.merged_path([str(a), str(b)]) == str(tmp_path / "cam" / "_merged" / "GX010001_merged.mp4")

    monkeypatch.setattr(merge, "_writable", {})
    monkeypatch.setattr(merge, "_can_write", lambda folder: False)
    out = merge.merged_path([str(a), str(b)])
    assert os.path.dirname(out) == os.path.join(str(tmp_path / "home"), "merged")
    assert out != merge.merged_path([str(other)])  # same name from another card doesn't clash

    # a merge left over from an earlier run is found wherever it is
    open(out, "wb").close()
    monkeypatch.setattr(merge, "_can_write", lambda folder: True)
    assert merge.merged_path([str(a), str(b)]) == out


def _compare_api(tmp_path, monkeypatch, videos, order=()):
    from gopro_uploader import app as app_mod
    from gopro_uploader import youtube
    monkeypatch.setattr(app_mod, "SAME_SIZE_MIN", 1)
    api, events, _ = _upload_api(tmp_path, monkeypatch, 1, None)
    calls = []

    def read(yt, pid, since="", on_page=None, cancel=None):
        calls.append((pid, since))
        return list(videos), True

    monkeypatch.setattr(youtube, "uploads_playlist", lambda yt: "UU")
    monkeypatch.setattr(youtube, "read_playlist", read)
    monkeypatch.setattr(youtube, "playlist_videos", lambda yt, pl: list(order))
    monkeypatch.setattr(youtube, "file_info", lambda yt, ids: {})
    return api, events, calls


def test_a_scan_does_not_touch_youtube(tmp_path, monkeypatch):
    from gopro_uploader import youtube

    def no(*a, **k):
        raise AssertionError("read YouTube without being asked")

    monkeypatch.setattr(youtube, "uploads_playlist", no)
    monkeypatch.setattr(youtube, "read_playlist", no)
    monkeypatch.setattr(youtube, "file_info", no)
    api, _, _ = _upload_api(tmp_path, monkeypatch, 1, None)
    assert api._items[0]["on_channel"] == []
    assert not os.path.exists(config.path("channel.json"))


def test_compare_marks_by_size_and_lists_the_rest(tmp_path, monkeypatch):
    from gopro_uploader.app import Api
    videos = [{"id": "OTHER", "title": "別的影片", "size": 123, "name": "x.mp4", "published": "2025-01-01T00:00:00Z"}]
    api, events, calls = _compare_api(tmp_path, monkeypatch, videos, order=["OTHER"])
    it = api._items[0]
    videos.append({"id": "SAME", "title": "手動傳的", "size": it["size"], "name": "renamed.mp4", "published": "2025-01-01T00:00:00Z"})

    res = api.compare("since", "2025-03-01")
    assert calls == [("UU", "2025-02-28T00:00:00Z")]
    assert [v["id"] for v in api._items[0]["on_channel"]] == ["SAME"]
    kinds = {r["kind"]: r for r in res["rows"]}
    assert kinds["uploaded"]["video"]["id"] == "SAME" and kinds["only"]["video"]["id"] == "OTHER"
    assert kinds["only"]["video"]["pos"] == 0 and res["target"] == "2025 日本"

    # kept for next time, until another account signs in or it is cleared
    again = Api("unused.log")
    assert again._channel[it["size"]][0]["id"] == "SAME"
    again.clear_compare()
    assert Api("unused.log")._channel == {}


def test_compare_a_same_named_file_of_another_size_is_only_a_maybe(tmp_path, monkeypatch):
    api, _, _ = _compare_api(tmp_path, monkeypatch, [])
    name = api._items[0]["files"][0]
    api._compared = {"videos": [{"id": "A", "title": "t", "size": 5, "name": name.lower(), "published": ""}]}
    assert [r["kind"] for r in api.compare_result()["rows"]] == ["maybe"]
    assert api._items[0]["on_channel"] == []
    api._compared = {"videos": []}
    assert [r["kind"] for r in api.compare_result()["rows"]] == ["none"]


def test_compare_tells_this_apps_other_uploads_from_unknown_videos(tmp_path, monkeypatch):
    api, _, _ = _compare_api(tmp_path, monkeypatch, [])
    api._store.data["another folder"] = {"video_ids": ["OURS"]}
    api._compared = {"videos": [{"id": "OURS", "title": "a", "size": 5, "name": "", "published": ""},
                                {"id": "HAND", "title": "b", "size": 6, "name": "", "published": ""}]}
    kinds = {r["video"]["id"]: r["kind"] for r in api.compare_result()["rows"] if r["video"]}
    assert kinds == {"OURS": "elsewhere", "HAND": "only"}


def test_verify_with_nothing_uploaded_does_not_say_all_is_well(tmp_path, monkeypatch):
    api, events, _ = _upload_api(tmp_path, monkeypatch, 1, None)
    api._channel = {api._items[0]["size"]: [{"id": "SAME"}]}
    api.verify()
    said = " / ".join(d.get("msg", "") for _, d in events)
    assert "沒有可以核對" in said and "沒有發現問題" not in said


def test_missing_client_secret_logs_what_could_be_seen_names_only(caplog):
    from gopro_uploader import youtube as yt_mod
    with open(config.path("settings.json"), "w", encoding="utf-8") as f:
        f.write("CONTENTS")
    with pytest.raises(RuntimeError, match="client_secret.json"):
        yt_mod.get_service()
    assert "settings.json" in caplog.text and "CONTENTS" not in caplog.text


def test_compare_can_read_one_playlist_or_be_cancelled(tmp_path, monkeypatch):
    from gopro_uploader import youtube
    api, _, calls = _compare_api(tmp_path, monkeypatch, [])
    api.compare("playlist", "", "PLX")
    assert calls == [("PLX", "")]
    monkeypatch.setattr(youtube, "read_playlist", lambda yt, pid, since="", on_page=None, cancel=None: ([], False))
    assert api.compare("all")["complete"] is False


def test_read_playlist_stops_at_the_first_page_older_than_the_date():
    from gopro_uploader import youtube

    pages = [["2025-05-02", "2025-04-30"], ["2025-03-01", "2025-02-01"], ["2024-01-01", "2024-01-01"]]

    class Req:
        def __init__(self, n):
            self.n = n

        def execute(self, **k):
            return {"items": [{"snippet": {"publishedAt": f"{d}T00:00:00Z"},
                               "contentDetails": {"videoId": d}} for d in pages[self.n]]}

    read = []

    class Items:
        def list(self, **k):
            return Req(0)

        def list_next(self, req, resp):
            read.append(req.n)
            return Req(req.n + 1) if req.n + 1 < len(pages) else None

    class Yt:
        def playlistItems(self):
            return Items()

    real = youtube.file_info
    youtube.file_info = lambda yt, ids: {i: {"id": i, "published": f"{i}T00:00:00Z"} for i in ids}
    try:
        got, done = youtube.read_playlist(Yt(), "UU", since="2025-04-01T00:00:00Z")
    finally:
        youtube.file_info = real
    assert [v["id"] for v in got] == ["2025-05-02", "2025-04-30"] and done
    assert read == [0]  # the second page was all older: the third is never asked for


def test_only_some_chapters_on_the_channel_and_its_own_upload_does_not_count(tmp_path, monkeypatch):
    from gopro_uploader.app import Api
    api = Api("unused.log")
    api._channel = {5_000_000: [{"id": "A"}], 7_000_000: [{"id": "MINE"}]}
    both = {"status": "pending", "video_ids": [], "sizes": [5_000_000, 7_000_000]}
    api._attach_channel(both)
    assert [v["id"] for v in both["on_channel"]] == ["A", "MINE"] and not both["on_channel_some"]
    some = {"status": "pending", "video_ids": [], "sizes": [5_000_000, 9_000_000]}
    api._attach_channel(some)
    assert some["on_channel_some"]
    own = {"status": "partial", "video_ids": ["MINE"], "sizes": [7_000_000, 9_000_000]}
    api._attach_channel(own)
    assert own["on_channel"] == []
    done = {"status": "uploaded", "video_ids": [], "sizes": [5_000_000]}
    api._attach_channel(done)
    assert done["on_channel"] == []


def test_small_files_are_not_matched_by_size(tmp_path):
    from gopro_uploader.app import Api
    assert Api._index_channel([{"id": "A", "size": 999}, {"id": "B", "size": 2_000_000}]).keys() == {2_000_000}


def _verify_api(tmp_path, monkeypatch, playlist, broken=()):
    from gopro_uploader import youtube
    api, events, _ = _upload_api(tmp_path, monkeypatch, 3, None)
    for k, it in enumerate(api._items, 1):
        api._store.update(it, status="uploaded", video_ids=[f"V{k}"])

    def info(yt, ids):
        out = {}
        for vid in ids:
            if vid in broken or not vid.startswith("V"):
                continue
            k = int(vid[1:])
            out[vid] = {"id": vid, "title": vid, "size": api._items[0]["size"], "name": "", "problem": "",
                        "upload_status": "processed", "published": f"2025-04-0{k}T00:00:00Z"}
        return out

    monkeypatch.setattr(youtube, "file_info", info)
    monkeypatch.setattr(youtube, "playlist_videos", lambda yt, pl: playlist)
    monkeypatch.setattr(youtube, "fetch_videos", lambda yt, ids: {})
    return api


def test_verify_says_all_is_well(tmp_path, monkeypatch):
    api = _verify_api(tmp_path, monkeypatch, ["V1", "V2", "V3"])
    res = api.verify()
    assert {c["level"] for c in res["checks"]} == {"ok"}
    texts = " / ".join(c["text"] for c in res["checks"])
    assert "3 支都在 YouTube 上" in texts and "順序和表格相同" in texts


def test_verify_finds_what_is_wrong(tmp_path, monkeypatch):
    api = _verify_api(tmp_path, monkeypatch, ["V2", "V1", "GONE"], broken={"V3"})
    res = api.verify()
    bad = [c["text"] for c in res["checks"] if c["level"] == "bad"]
    assert any("1 支在 YouTube 上找不到" in t for t in bad)
    assert any("1 支不在播放清單" in t for t in bad)
    assert any("順序不對" in t for t in bad)
    assert any("1 個已刪除" in c["text"] and c["level"] == "warn" for c in res["checks"])


def test_verify_compares_each_file_size_but_not_merged_ones(tmp_path, monkeypatch):
    from gopro_uploader.app import Api
    api = Api("unused.log")
    row = dict(title="T", files=["GX010001.MP4"], video_ids=["V1"], sizes=[100], status="uploaded")
    got = {"V1": {"size": 99, "upload_status": "processed", "problem": ""}}
    assert any("大小和本機不同" in c["text"] for c in api._verify_videos([row], got))
    merged = dict(row, files=["GX010001.MP4", "GX020001.MP4"], sizes=[60, 40])
    checks = api._verify_videos([merged], got)
    assert not any("大小和本機不同" in c["text"] for c in checks) and "1 段合併過" in checks[0]["text"]
    assert any(c["level"] == "warn" for c in api._verify_videos(
        [row], {"V1": {"size": 100, "upload_status": "failed", "problem": "codec"}}))


def test_verify_uses_the_playlist_it_was_uploaded_for_not_the_one_picked_now(tmp_path, monkeypatch):
    from gopro_uploader import youtube
    api = _verify_api(tmp_path, monkeypatch, [])
    for k, it in enumerate(api._items, 1):
        api._store.append_history(it, f"V{k}", it["title"], "以前的清單")
    monkeypatch.setattr(youtube, "list_playlists", lambda yt: [{"id": "OLD", "title": "以前的清單"}])
    asked = []
    monkeypatch.setattr(youtube, "playlist_videos", lambda yt, pl: asked.append(pl) or ["V1", "V2", "V3"])
    res = api.verify()
    assert asked == ["OLD"] and {c["level"] for c in res["checks"]} == {"ok"}

    monkeypatch.setattr(youtube, "list_playlists", lambda yt: [])
    assert any("找不到播放清單「以前的清單」" in c["text"] for c in api.verify()["checks"])
