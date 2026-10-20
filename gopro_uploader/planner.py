"""Turn scanned recordings + settings + user edits into an upload plan."""
from __future__ import annotations

import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .scanner import Recording

NEARBY_WINDOW = timedelta(hours=3)


def detect_camera_offset(recs: list[Recording]) -> timedelta | None:
    """Camera clock minus true UTC, from clips that have both a clock time and a GPS fix."""
    diffs = []
    for r in recs:
        for c in r.clips:
            info = c.info
            if info and info.camera_time and info.first_fix and info.first_fix.utc:
                fix = info.first_fix
                utc_start = fix.utc.replace(tzinfo=None) - timedelta(seconds=fix.t)
                diffs.append((info.camera_time - utc_start).total_seconds())
    if not diffs:
        return None
    return timedelta(seconds=statistics.median(diffs))


def file_offset(rec: Recording) -> timedelta | None:
    """The camera's UTC offset while recording, from the first clip's file times.

    Newer GoPros write mvhd in UTC but stamp the file with the camera's local clock,
    so (file time - duration) - mvhd is the zone the camera was set to (+8 in Taiwan,
    +9 in Japan). None when that doesn't look like a real offset,
    e.g. a camera that writes mvhd in local time (difference ~0).
    """
    c = rec.clips[0]
    if not (c.info and c.info.camera_time):
        return None
    start = datetime.fromtimestamp(c.mtime) - timedelta(seconds=c.info.duration)
    diff = (start - c.info.camera_time).total_seconds()
    q = round(diff / 900) * 900  # real zones are whole quarter hours
    if q == 0 or abs(diff - q) > 120 or abs(q) > 14 * 3600:
        return None
    return timedelta(seconds=q)


def _zone_for(offset: timedelta, at: datetime, candidates) -> ZoneInfo | timezone:
    """A named zone with that offset at that moment (so titles say JST), else a fixed offset."""
    for tz in candidates:
        if at.astimezone(tz).utcoffset() == offset:
            return tz
    return timezone(offset)


def _rec_utc(rec: Recording, cam_offset: timedelta | None, camera_tz: ZoneInfo | None,
             trip_tz: ZoneInfo, file_off: timedelta | None = None):
    """Return (utc datetime, source)."""
    elapsed = 0.0
    for c in rec.clips:
        fix = c.info.first_fix if c.info else None
        if fix and fix.utc:
            return fix.utc - timedelta(seconds=fix.t + elapsed), "gps"
        elapsed += c.info.duration if c.info else 0

    first = rec.clips[0]
    cam = first.info.camera_time if first.info else None
    if cam:
        if camera_tz:
            return cam.replace(tzinfo=camera_tz).astimezone(timezone.utc), "camera"
        if file_off is not None:  # file times prove mvhd is UTC
            return cam.replace(tzinfo=timezone.utc), "camera"
        if cam_offset is not None:
            return (cam - cam_offset).replace(tzinfo=timezone.utc), "camera+gps_offset"
        return cam.replace(tzinfo=trip_tz).astimezone(timezone.utc), "camera"
    # last resort: file modified time marks the end of the last chapter
    total = sum(c.info.duration for c in rec.clips if c.info)
    end = datetime.fromtimestamp(rec.clips[-1].mtime, tz=timezone.utc)
    return end - timedelta(seconds=total), "file_mtime"


def build_plan(recs: list[Recording], settings: Settings, overrides: dict,
               store_lookup=None, place_lookup=None) -> list[dict]:
    trip_tz = ZoneInfo(settings.trip_tz)
    home_tz = ZoneInfo(settings.home_tz)
    camera_tz = None if settings.camera_tz == "auto" else ZoneInfo(settings.camera_tz)
    cam_offset = detect_camera_offset(recs) if camera_tz is None else None

    # A camera whose clock was reset stamps the same start time on every clip it records.
    cam_times = [r.clips[0].info.camera_time if r.clips[0].info else None for r in recs]
    shared = {t for t in cam_times if t is not None and cam_times.count(t) > 1}

    items = []
    for rec, cam_time in zip(recs, cam_times):
        off = file_offset(rec) if camera_tz is None else None
        utc, tsrc = _rec_utc(rec, cam_offset, camera_tz, trip_tz, off)
        ov = overrides.get(rec.key, {})
        # the zone this recording was shot in: user's pick > camera's own clock > trip default
        if ov.get("tz"):
            zone, zsrc = ZoneInfo(ov["tz"]), "manual"
        elif off is not None:
            zone, zsrc = _zone_for(off, utc, (home_tz, trip_tz)), "file"
        else:
            zone, zsrc = trip_tz, "default"
        if ov.get("local_time"):  # "YYYY-MM-DDTHH:MM" in the recording's zone, set by the user
            utc = datetime.fromisoformat(ov["local_time"]).replace(tzinfo=zone).astimezone(timezone.utc)
            tsrc = "manual"
        local = utc.astimezone(zone)
        track = [[p.lat, p.lon] for c in rec.clips if c.info for p in c.info.gps]
        names = [c.name for c in rec.clips]
        item = {
            "id": rec.key,
            "files": names,
            "paths": [c.path for c in rec.clips],
            "folder": os.path.dirname(rec.clips[0].path),
            "size": sum(c.size for c in rec.clips),
            "duration": round(sum(c.info.duration for c in rec.clips if c.info), 1),
            "utc": utc.isoformat(),
            "local": local.strftime("%Y-%m-%d %H:%M:%S"),
            "tz": getattr(zone, "key", None) or local.strftime("%Z"),
            "tz_source": zsrc,
            # no label in the home zone (e.g. Taiwan); otherwise "JST" etc.
            "tz_label": "" if local.utcoffset() == local.astimezone(home_tz).utcoffset()
                        else local.strftime("%Z"),
            "time_source": tsrc,
            "time_suspect": tsrc not in ("gps", "manual") and cam_time in shared,
            "lat": track[0][0] if track else None,
            "lon": track[0][1] if track else None,
            "loc_source": "gps" if track else None,
            "track": track,
            "place": "",
            # don't upload a wrong date by accident: suspect times start unticked
            "include": not (tsrc not in ("gps", "manual") and cam_time in shared),
            "scan_errors": [f"{c.name}: {c.error}" for c in rec.clips if c.error],
        }
        items.append(item)

    _fill_nearby_locations(items)

    for item in items:
        ov = overrides.get(item["id"], {})
        for k in ("lat", "lon", "place", "include"):
            if k in ov:
                item[k] = ov[k]
        if "lat" in ov:
            item["loc_source"] = "manual"
        if "place" not in ov and item["lat"] is not None and place_lookup:
            item["place"] = place_lookup(item["lat"], item["lon"]) or ""
        local = datetime.fromisoformat(item["local"])
        default_title = local.strftime(settings.title_format)
        if item["tz_label"]:
            default_title += " " + item["tz_label"]
        item["title"] = ov.get("title") or default_title
        item["title_locked"] = "title" in ov
        item["title_date_mismatch"] = item["title_locked"] and _date_mismatch(item["title"], local)
        item["edited"] = sorted(k for k in ov if k != "include")  # for "已修改" badges in the UI
        item["description"] = ov.get("description") or settings.description_template.format(
            files="\n".join(item["files"]), place=item["place"])
        # YouTube keeps only the date; send local midnight as UTC so the day never shifts
        item["recording_date"] = local.strftime("%Y-%m-%dT00:00:00Z")
        rec_state = store_lookup(item) if store_lookup else None
        item["status"] = rec_state["status"] if rec_state else "pending"
        item["video_ids"] = rec_state.get("video_ids", []) if rec_state else []
        if item["status"] == "uploaded" and "include" not in ov:
            item["include"] = False

    _dedupe_titles(items)
    return items


_DATE_IN_TITLE = re.compile(r"(\d{4})\s*[-/年.]?\s*(\d{1,2})\s*[-/月.]?\s*(\d{1,2})")


def _date_mismatch(title: str, local: datetime) -> bool:
    """A hand-typed title holds a date, but not the recording's date (e.g. typo 04-25 vs 04-04)."""
    found = [(int(y), int(m), int(d)) for y, m, d in _DATE_IN_TITLE.findall(title)]
    return bool(found) and (local.year, local.month, local.day) not in found


def _fill_nearby_locations(items):
    with_gps = [i for i in items if i["lat"] is not None]
    for item in items:
        if item["lat"] is not None or not with_gps:
            continue
        t = datetime.fromisoformat(item["utc"])
        best = min(with_gps, key=lambda g: abs(datetime.fromisoformat(g["utc"]) - t))
        if abs(datetime.fromisoformat(best["utc"]) - t) <= NEARBY_WINDOW:
            item["lat"], item["lon"] = best["lat"], best["lon"]
            item["loc_source"] = "nearby"


def _dedupe_titles(items):
    seen: dict[str, int] = {}
    for item in items:
        n = seen.get(item["title"], 0) + 1
        seen[item["title"]] = n
        if n > 1 and not item["title_locked"]:
            item["title"] = f"{item['title']} ({n})"
