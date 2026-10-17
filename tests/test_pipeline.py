import os
from datetime import datetime, timezone

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
    assert a["title"] == "2026-03-15 14:32"
    assert a["time_source"] == "gps"
    assert a["description"] == "GX010001.MP4\nGX020001.MP4"
    assert a["recording_date"] == "2026-03-15T00:00:00Z"
    assert a["loc_source"] == "gps"
    # no GPS: time from camera clock corrected by the GPS-derived offset, place inferred
    assert b["title"] == "2026-03-15 14:52"
    assert b["time_source"] == "camera+gps_offset"
    assert b["loc_source"] == "nearby"
    assert b["lat"] == pytest.approx(SAMPLE_PLACE[0], abs=1e-6)


def test_camera_clock_in_taiwan_time_is_corrected(tmp_path):
    # camera still on Taiwan time (1h behind Japan) -> titles should be Japan local
    tw_cam = CAM.replace(hour=13)
    make_mp4(tmp_path / "GX010001.MP4", tw_cam, 4.0, [gps5_payload(*SAMPLE_PLACE, "260315053210.000")] * 4)
    make_mp4(tmp_path / "GX010002.MP4", tw_cam.replace(minute=50), 4.0, [])
    items = build_plan(scan(str(tmp_path)), config.Settings(), {})
    assert [i["title"] for i in items] == ["2026-03-15 14:32", "2026-03-15 14:50"]


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
    assert [i["title"] for i in items] == ["2026-03-15 14:32", "2026-03-15 14:32 (2)"]
    key = items[0]["id"]
    items = build_plan(recs, config.Settings(), {key: {"title": "範例寺", "lat": 1.0, "lon": 2.0,
                                                       "place": "X"}})
    assert items[0]["title"] == "範例寺" and items[0]["loc_source"] == "manual"
    assert items[0]["place"] == "X"
