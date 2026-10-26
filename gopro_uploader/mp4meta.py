"""Read creation time, duration and GoPro GPS (GPMF) from MP4 files.

Pure Python, no ffprobe/exiftool needed. Only the `moov` box is loaded into
memory; GPS samples are read with targeted seeks so large files on an HDD
are still quick to scan.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

MP4_EPOCH = datetime(1904, 1, 1)
GPS9_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# GPMF numeric type -> struct format char
_GPMF_TYPES = {
    "b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I",
    "f": "f", "d": "d", "j": "q", "J": "Q",
}

# Read every Nth GPS sample (≈1 per second) until a fix is found, then
# every TRACK_STRIDE samples for the map track. Keeps HDD seeks low.
SEARCH_STRIDE = 3
TRACK_STRIDE = 20


@dataclass
class GpsPoint:
    t: float                 # seconds from clip start
    lat: float
    lon: float
    utc: datetime | None     # GPS (true UTC) time, if present


@dataclass
class Mp4Info:
    camera_time: datetime | None   # naive, as written by the camera clock
    duration: float
    gps: list[GpsPoint] = field(default_factory=list)
    # (width, height, rotation degrees) of the video track; None = not read yet (old cache)
    dims: tuple[int, int, int] | None = None

    @property
    def vertical(self) -> bool:
        """Shown upright taller than wide (camera held sideways writes a 90° rotation)."""
        if not self.dims:
            return False
        w, h, rot = self.dims
        return (h > w) != (rot in (90, 270))

    @property
    def first_fix(self) -> GpsPoint | None:
        return self.gps[0] if self.gps else None


# ---------------------------------------------------------------- MP4 boxes

def _children(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hlen = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen or pos + size > end:
            break
        yield typ, pos + hlen, pos + size
        pos += size


def _find(buf: bytes, start: int, end: int, *path: bytes):
    for typ, s, e in _children(buf, start, end):
        if typ == path[0]:
            if len(path) == 1:
                return s, e
            found = _find(buf, s, e, *path[1:])
            if found:
                return found
    return None


def _read_moov(f) -> bytes | None:
    f.seek(0, 2)
    end = f.tell()
    pos = 0
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(16)
        size, typ = struct.unpack_from(">I4s", hdr)
        hlen = 8
        if size == 1:
            size = struct.unpack_from(">Q", hdr, 8)[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen:
            break
        if typ == b"moov":
            f.seek(pos + hlen)
            return f.read(size - hlen)
        pos += size
    return None


def _full_box_entries(buf, s, fmt):
    count = struct.unpack_from(">I", buf, s + 4)[0]
    step = struct.calcsize(fmt)
    return [struct.unpack_from(fmt, buf, s + 8 + i * step) for i in range(count)]


def _sample_table(buf: bytes, trak_s: int, trak_e: int):
    """Return [(offset, size, start_seconds)] for every sample in a track."""
    mdhd = _find(buf, trak_s, trak_e, b"mdia", b"mdhd")
    stbl = _find(buf, trak_s, trak_e, b"mdia", b"minf", b"stbl")
    if not mdhd or not stbl:
        return []
    ms = mdhd[0]
    timescale = struct.unpack_from(">I", buf, ms + (20 if buf[ms] == 1 else 12))[0] or 1
    ss, se = stbl

    stsz = _find(buf, ss, se, b"stsz")
    stsc = _find(buf, ss, se, b"stsc")
    stts = _find(buf, ss, se, b"stts")
    stco = _find(buf, ss, se, b"stco")
    co64 = _find(buf, ss, se, b"co64")
    if not (stsz and stsc and (stco or co64)):
        return []

    fixed, count = struct.unpack_from(">II", buf, stsz[0] + 4)
    sizes = [fixed] * count if fixed else list(
        struct.unpack_from(f">{count}I", buf, stsz[0] + 12))
    chunks = [c[0] for c in (_full_box_entries(buf, stco[0], ">I") if stco
                             else _full_box_entries(buf, co64[0], ">Q"))]
    stsc_e = _full_box_entries(buf, stsc[0], ">III")

    times = []
    if stts:
        t = 0
        for n, delta in _full_box_entries(buf, stts[0], ">II"):
            for _ in range(n):
                times.append(t / timescale)
                t += delta

    out, si, idx = [], 0, 0
    for ci, off in enumerate(chunks, start=1):
        while idx + 1 < len(stsc_e) and stsc_e[idx + 1][0] <= ci:
            idx += 1
        for _ in range(stsc_e[idx][1] if stsc_e else 0):
            if si >= len(sizes):
                break
            out.append((off, sizes[si], times[si] if si < len(times) else 0.0))
            off += sizes[si]
            si += 1
    return out


# --------------------------------------------------------------------- GPMF

def _klv(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        key = data[pos:pos + 4]
        if key == b"\0\0\0\0":
            break
        typ, ssize, rep = struct.unpack_from(">cBH", data, pos + 4)
        length = ssize * rep
        s = pos + 8
        yield key, typ.decode("latin1"), ssize, rep, s, min(s + length, end)
        pos = s + ((length + 3) & ~3)


def _numbers(data, typ, ssize, rep, s):
    fc = _GPMF_TYPES.get(typ)
    if not fc:
        return []
    n = ssize // struct.calcsize(fc)
    return [struct.unpack_from(f">{n}{fc}", data, s + i * ssize) for i in range(rep)]


def _scale(scal, i):
    if not scal:
        return 1
    v = scal[i] if len(scal) > 1 and i < len(scal) else scal[0]
    return v or 1


def parse_gpmf_gps(data: bytes):
    """Return (lat, lon, utc, fix, dop) for the first GPS entry in a payload."""
    for key, _, _, _, s, e in _klv(data, 0, len(data)):
        if key != b"DEVC":
            continue
        for k2, _, _, _, s2, e2 in _klv(data, s, e):
            if k2 != b"STRM":
                continue
            scal, gpsu, fix, dop, type_def = [], None, None, None, None
            for k3, typ, ssize, rep, s3, e3 in _klv(data, s2, e2):
                if k3 == b"SCAL":
                    scal = [v[0] for v in _numbers(data, typ, ssize, rep, s3)] if ssize <= 8 \
                        else list(_numbers(data, typ, ssize, rep, s3)[0])
                elif k3 == b"GPSU":
                    gpsu = data[s3:e3].decode("ascii", "ignore")[:16]
                elif k3 == b"GPSF":
                    fix = _numbers(data, typ, ssize, rep, s3)[0][0]
                elif k3 == b"GPSP":
                    dop = _numbers(data, typ, ssize, rep, s3)[0][0] / 100
                elif k3 == b"TYPE":
                    type_def = data[s3:e3].decode("ascii", "ignore").rstrip("\0")
                elif k3 == b"GPS5" and rep:
                    v = _numbers(data, typ, ssize, rep, s3)[0]
                    utc = _parse_gpsu(gpsu)
                    return (v[0] / _scale(scal, 0), v[1] / _scale(scal, 1), utc, fix, dop)
                elif k3 == b"GPS9" and rep and type_def:
                    v = _unpack_complex(data, s3, type_def)
                    vals = [x / _scale(scal, i) for i, x in enumerate(v)]
                    utc = GPS9_EPOCH + timedelta(days=vals[5], seconds=vals[6])
                    return (vals[0], vals[1], utc, vals[8], vals[7])
    return None


def _unpack_complex(data, s, type_def):
    fmt = ">" + "".join(_GPMF_TYPES[c] for c in type_def)
    return struct.unpack_from(fmt, data, s)


def _parse_gpsu(text):
    if not text:
        return None
    try:
        return datetime.strptime(text[:16], "%y%m%d%H%M%S.%f").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _valid_fix(r) -> bool:
    if not r:
        return False
    lat, lon, _, fix, dop = r
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return False
    if fix is not None and fix < 2:
        return False
    if dop is not None and dop > 10:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _video_dims(moov: bytes) -> tuple[int, int, int] | None:
    """(width, height, rotation) from the video track's tkhd."""
    for typ, s, e in _children(moov, 0, len(moov)):
        if typ != b"trak":
            continue
        hdlr = _find(moov, s, e, b"mdia", b"hdlr")
        tkhd = _find(moov, s, e, b"tkhd")
        if not hdlr or not tkhd or moov[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
            continue
        end = tkhd[1]
        w, h = struct.unpack_from(">II", moov, end - 8)
        a, b = struct.unpack_from(">ii", moov, end - 44)   # first row of the 3x3 matrix
        rot = {(0, 65536): 90, (0, -65536): 270, (-65536, 0): 180}.get((a, b), 0)
        return w >> 16, h >> 16, rot
    return None


def read_video_dims(path: str) -> tuple[int, int, int] | None:
    """Only the moov box: cheap, used to fill in old cache entries."""
    with open(path, "rb") as f:
        moov = _read_moov(f)
    return _video_dims(moov) if moov else None


# ---------------------------------------------------------------------- API

def read_mp4(path: str) -> Mp4Info:
    with open(path, "rb") as f:
        moov = _read_moov(f)
        if moov is None:
            raise ValueError("找不到 moov box（檔案可能損毀或尚未複製完成）")
        end = len(moov)

        camera_time, duration = None, 0.0
        mvhd = _find(moov, 0, end, b"mvhd")
        if mvhd:
            s = mvhd[0]
            if moov[s] == 1:
                ctime, _, ts, dur = struct.unpack_from(">QQIQ", moov, s + 4)
            else:
                ctime, _, ts, dur = struct.unpack_from(">IIII", moov, s + 4)
            if ctime:
                camera_time = MP4_EPOCH + timedelta(seconds=ctime)
            duration = dur / ts if ts else 0.0

        gps: list[GpsPoint] = []
        for typ, ts_, te in _children(moov, 0, end):
            if typ != b"trak":
                continue
            stsd = _find(moov, ts_, te, b"mdia", b"minf", b"stbl", b"stsd")
            if not stsd or moov[stsd[0] + 12:stsd[0] + 16] != b"gpmd":
                continue
            samples = _sample_table(moov, ts_, te)
            i = 0
            while i < len(samples):
                off, size, t = samples[i]
                f.seek(off)
                r = parse_gpmf_gps(f.read(size))
                if _valid_fix(r):
                    gps.append(GpsPoint(t, r[0], r[1], r[2]))
                i += TRACK_STRIDE if gps else SEARCH_STRIDE
            # make sure the last fix is included so the track reaches the end
            if gps and samples:
                off, size, t = samples[-1]
                f.seek(off)
                r = parse_gpmf_gps(f.read(size))
                if _valid_fix(r) and t > gps[-1].t:
                    gps.append(GpsPoint(t, r[0], r[1], r[2]))
            break
        return Mp4Info(camera_time, duration, gps, _video_dims(moov))
