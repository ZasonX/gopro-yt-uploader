import os
from datetime import datetime, timedelta, timezone

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


def test_uploaded_recordings_cannot_be_edited(tmp_path):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010797.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    item = api.scan(str(tmp_path))["items"][0]
    api._store.update(item, status="uploaded", video_ids=["VIDEO_ID_01"])
    api._replan()
    res = api.edit([item["id"]], {"title": "改了也沒用"})
    assert res["items"][0]["title"] == "2026-03-15 14:32 JST" and res["items"][0]["edited"] == []


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
    api._uploading_id = a["id"]
    api.edit([b["id"]], {"title": "往出口移動"})  # re-plan during the upload
    worker_item = next(i for i in api._items if i["id"] == a["id"])
    worker_item["video_ids"] = ["VIDEO_ID_02"]
    api._set_status(worker_item, "uploaded")
    api._uploading_id = None

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
