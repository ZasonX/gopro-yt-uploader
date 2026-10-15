"""Turn scanned recordings + settings + user edits into an upload plan."""
from __future__ import annotations

import os
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


def _rec_utc(rec: Recording, cam_offset: timedelta | None, camera_tz: ZoneInfo | None,
             trip_tz: ZoneInfo):
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
    camera_tz = None if settings.camera_tz == "auto" else ZoneInfo(settings.camera_tz)
    cam_offset = detect_camera_offset(recs) if camera_tz is None else None

    items = []
    for rec in recs:
        utc, tsrc = _rec_utc(rec, cam_offset, camera_tz, trip_tz)
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
            "local": utc.astimezone(trip_tz).strftime("%Y-%m-%d %H:%M:%S"),
            "time_source": tsrc,
            "lat": track[0][0] if track else None,
            "lon": track[0][1] if track else None,
            "loc_source": "gps" if track else None,
            "track": track,
            "place": "",
            "include": True,
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
        local = datetime.fromisoformat(item["utc"]).astimezone(trip_tz)
        item["title"] = ov.get("title") or local.strftime(settings.title_format)
        item["title_locked"] = "title" in ov
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
