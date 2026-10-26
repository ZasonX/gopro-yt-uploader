"""Turn scanned recordings + settings + user edits into an upload plan."""
from __future__ import annotations

import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .scanner import Recording, gopro_number, group_key

NEARBY_WINDOW = timedelta(hours=3)
SHORTS_MAX_SECONDS = 180


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
        suspect = tsrc not in ("gps",) and cam_time in shared
        time_known = not suspect
        if ov.get("local_time"):  # "YYYY-MM-DDTHH:MM", or "YYYY-MM-DD" = date only, in the recording's zone
            lt = ov["local_time"]
            time_known = len(lt) > 10
            utc = datetime.fromisoformat(lt if time_known else lt + "T12:00").replace(
                tzinfo=zone).astimezone(timezone.utc)
            tsrc, suspect = "manual", False
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
            # the camera clock was reset: no real time in the file; the date is guessed below
            "time_suspect": suspect,
            "time_known": time_known,
            "date_guess": "",           # filled for suspects by _guess_dates
            "date_uncertain": False,
            "lat": track[0][0] if track else None,
            "lon": track[0][1] if track else None,
            "loc_source": "gps" if track else None,
            "track": track,
            "place": "",
            # opt-in: nothing is uploaded unless the user ticks it (a folder can hold
            # hundreds of clips; ticking all by default made "開始上傳" a trap)
            "include": False,
            "scan_errors": [f"{c.name}: {c.error}" for c in rec.clips if c.error],
            "number": gopro_number(names[0]),
            # YouTube makes vertical (or square) videos up to 3 minutes a Short
            "vertical": bool(rec.clips[0].info and rec.clips[0].info.vertical),
        }
        item["short"] = item["vertical"] and item["duration"] <= SHORTS_MAX_SECONDS
        items.append(item)

    _guess_dates(items)
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
        if item["time_known"]:
            default_title = local.strftime(settings.title_format)
            if item["tz_label"]:
                default_title += " " + item["tz_label"]
        else:  # no real time: the date alone, e.g. "2025-04-04" (no zone label either)
            default_title = local.strftime(date_part(settings.title_format))
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


def date_part(fmt: str) -> str:
    """The date-only part of a title format: "%Y-%m-%d %H:%M" -> "%Y-%m-%d"."""
    i = fmt.find("%H")
    return fmt[:i].rstrip(" T_-") if i > 0 else fmt


def _gopro_number(item) -> tuple[str, str]:
    """Shooting order within a folder: GoPro numbers files sequentially (GX010797 -> 0797)."""
    key, _ = group_key(item["files"][0])
    return item["folder"], re.sub(r"\D", "", key[-4:]) or key


def _guess_dates(items):
    """Clock-reset recordings get the date of the nearest good recordings before / after.

    By file number, the previous and next recording with a real time bound it. Both on the
    same day -> that day. Different days (e.g. across midnight) -> the later day, flagged.
    Only one side -> its day. The time stays unknown, so the title shows the date alone.
    """
    order = sorted(items, key=_gopro_number)
    good = [i for i, it in enumerate(order) if not it["time_suspect"]]
    for i, it in enumerate(order):
        if not it["time_suspect"]:
            continue
        prev = next((order[j] for j in reversed(good) if j < i and order[j]["folder"] == it["folder"]), None)
        nxt = next((order[j] for j in good if j > i and order[j]["folder"] == it["folder"]), None)
        days = [x["local"][:10] for x in (prev, nxt) if x]
        if not days:
            continue  # nothing to go by: keep the camera's date, still flagged as suspect
        day = days[-1]
        it["date_guess"] = day
        it["date_uncertain"] = len(set(days)) > 1
        # midday of that day, in the recording's own offset (only the date is meaningful)
        offset = datetime.fromisoformat(it["local"]) - datetime.fromisoformat(it["utc"]).replace(tzinfo=None)
        new_local = datetime.fromisoformat(f"{day}T12:00:00")
        it["local"] = new_local.strftime("%Y-%m-%d %H:%M:%S")
        it["utc"] = (new_local - offset).replace(tzinfo=timezone.utc).isoformat()


_DATE_IN_TITLE = re.compile(r"(\d{4})\s*[-/年.]?\s*(\d{1,2})\s*[-/月.]?\s*(\d{1,2})")


_LEADING_DATE = re.compile(
    r"^(?P<y>\d{4})(?P<s1>\s*[-/年.]?\s*)(?P<m>\d{1,2})(?P<s2>\s*[-/月.]?\s*)(?P<d>\d{1,2})(?P<s3>日?)"
    r"(?P<time>\s+\d{1,2}:\d{2})?")


def retime_title(title: str, local: datetime, with_time: bool = True) -> str:
    """Put the recording's date (and time, if the title had one) at the start of a typed title.

    "2022-05-25 走到出口" + 2025-04-04 13:10 -> "2025-04-04 走到出口";
    with_time=False (time unknown) drops a time the title had: "2022-05-25 01:10 X" -> "2025-04-04 X".
    The user's own words after the date are never touched.
    """
    m = _LEADING_DATE.match(title)
    if not m:
        return title
    pad = lambda v, src: f"{v:02d}" if len(src) == 2 else str(v)  # noqa: E731 - keep their style
    new = f"{local.year}{m['s1']}{pad(local.month, m['m'])}{m['s2']}{pad(local.day, m['d'])}{m['s3']}"
    if m["time"] and with_time:
        new += m["time"][:len(m["time"]) - len(m["time"].lstrip())] + local.strftime("%H:%M")
    return new + title[m.end():]


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
