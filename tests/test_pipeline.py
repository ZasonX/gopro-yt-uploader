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
    assert not any(items[f"GX010{n}.MP4"]["include"] for n in (797, 798, 799))
    assert not items["GX010827.MP4"]["time_suspect"] and items["GX010827.MP4"]["include"]

    key = items["GX010798.MP4"]["id"]
    items = {i["files"][0]: i for i in build_plan(
        recs, config.Settings(), {key: {"local_time": "2025-04-03T21:05"}})}
    fixed = items["GX010798.MP4"]
    assert fixed["title"] == "2025-04-03 21:05 JST" and fixed["time_source"] == "manual"
    assert fixed["recording_date"] == "2025-04-03T00:00:00Z" and fixed["utc"].startswith("2025-04-03T12:05")
    assert not fixed["time_suspect"] and fixed["include"]


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

    api.edit([item["id"]], {"include": False})
    res = api.reset_edits([item["id"]])
    it = res["items"][0]
    assert it["edited"] == [] and it["place"] == "" and it["time_source"] != "manual"
    assert it["include"] is False  # the tick is not an "edit" to undo
    assert Api("unused.log").scan(str(tmp_path))["items"][0]["edited"] == []  # and it's saved


def test_title_edit_persists_across_restarts_but_ticks_do_not(tmp_path):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010001.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    item = api.scan(str(tmp_path))["items"][0]
    assert item["title"] == "2026-03-15 14:32 JST"  # default, ready to be edited
    api.edit([item["id"]], {"title": "2026-03-15 14:32 JST 範例標題 (測試)", "include": False})

    item = Api("unused.log").scan(str(tmp_path))["items"][0]  # fresh app start
    assert item["title"] == "2026-03-15 14:32 JST 範例標題 (測試)" and item["title_locked"]
    assert item["include"] is True  # a remembered tick could skip or re-upload videos

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


def test_set_location_updates_then_reads_back():
    from gopro_uploader import youtube

    class Req:
        def __init__(self, result):
            self.result = result

        def execute(self, num_retries=0):
            return self.result

    class Videos:
        def __init__(self):
            self.sent = None

        def update(self, part, body):
            self.sent = (part, body)
            return Req(body)

        def list(self, id, part):
            return Req({"items": [{"recordingDetails": self.sent[1]["recordingDetails"]}]})

    class YT:
        v = Videos()

        def videos(self):
            return self.v

    yt = YT()
    assert youtube.set_location(yt, "vid", "2025-04-04T00:00:00Z", 12.3456, 22.6543, "車站大廳")
    part, body = yt.v.sent
    assert part == "recordingDetails" and body["id"] == "vid"
    assert body["recordingDetails"] == {"recordingDate": "2025-04-04T00:00:00Z",
                                        "location": {"latitude": 12.3456, "longitude": 22.6543},
                                        "locationDescription": "車站大廳"}


def test_uploaded_recordings_cannot_be_edited(tmp_path):
    from gopro_uploader.app import Api
    make_mp4(tmp_path / "GX010797.MP4", CAM, 4.0, [])
    api = Api("unused.log")
    item = api.scan(str(tmp_path))["items"][0]
    api._store.update(item, status="uploaded", video_ids=["VIDEO_ID_01"])
    api._replan()
    res = api.edit([item["id"]], {"title": "改了也沒用"})
    assert res["items"][0]["title"] == "2026-03-15 14:32 JST" and res["items"][0]["edited"] == []
