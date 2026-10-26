"""Build tiny MP4 files that look like GoPro output (mvhd + gpmd GPS track)."""
from __future__ import annotations

import struct
from datetime import datetime

MP4_EPOCH = datetime(1904, 1, 1)


def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def full(typ: bytes, payload: bytes, ver: int = 0) -> bytes:
    return box(typ, bytes([ver, 0, 0, 0]) + payload)


def klv(key: bytes, typ: str, ssize: int, rep: int, data: bytes) -> bytes:
    data = data + b"\0" * ((-len(data)) % 4)
    return key + struct.pack(">cBH", typ.encode(), ssize, rep) + data


def gps5_payload(lat, lon, gpsu: str, fix=3, dop=150) -> bytes:
    strm = (klv(b"GPSF", "L", 4, 1, struct.pack(">I", fix))
            + klv(b"GPSU", "U", 16, 1, gpsu.encode())
            + klv(b"GPSP", "S", 2, 1, struct.pack(">H", dop))
            + klv(b"SCAL", "l", 4, 5, struct.pack(">5i", 10_000_000, 10_000_000, 1000, 1000, 100))
            + klv(b"GPS5", "l", 20, 1, struct.pack(">5i", round(lat * 1e7), round(lon * 1e7), 50000, 0, 0)))
    devc = klv(b"STRM", "\0", 1, len(strm), strm)
    return klv(b"DEVC", "\0", 1, len(devc), devc)


def gps9_payload(lat, lon, days, secs, fix=3) -> bytes:
    scal = [10_000_000, 10_000_000, 1000, 1000, 100, 1, 1000, 100, 1]
    strm = (klv(b"TYPE", "c", 1, 9, b"lllllllSS")
            + klv(b"SCAL", "l", 4, 9, struct.pack(">9i", *scal))
            + klv(b"GPS9", "?", 32, 1, struct.pack(">7i2H", round(lat * 1e7), round(lon * 1e7),
                                                   0, 0, 0, days, round(secs * 1000), 120, fix)))
    devc = klv(b"STRM", "\0", 1, len(strm), strm)
    return klv(b"DEVC", "\0", 1, len(devc), devc)


def video_trak(width=3840, height=2160, rotation=0) -> bytes:
    """A bare video track: just tkhd (size + rotation matrix) and hdlr 'vide'."""
    one = 0x10000
    a, b, c, d = {0: (one, 0, 0, one), 90: (0, one, -one, 0),
                  180: (-one, 0, 0, -one), 270: (0, -one, one, 0)}[rotation]
    matrix = struct.pack(">9i", a, b, 0, c, d, 0, 0, 0, 0x40000000)
    tkhd = full(b"tkhd", struct.pack(">IIIII", 0, 0, 1, 0, 0) + b"\0" * 8
                + struct.pack(">hhhH", 0, 0, 0, 0) + matrix + struct.pack(">II", width << 16, height << 16))
    hdlr = full(b"hdlr", b"\0\0\0\0vide" + b"\0" * 12 + b"GoPro AVC\0")
    return box(b"trak", tkhd + box(b"mdia", hdlr))


def make_mp4(path, camera_time: datetime, duration: float, gps_samples: list[bytes], video=None):
    """video: None, or (width, height, rotation) to add a video track."""
    ctime = int((camera_time - MP4_EPOCH).total_seconds())
    ftyp = box(b"ftyp", b"mp41\0\0\0\0mp41")
    mdat_payload = b"".join(gps_samples)
    mdat = box(b"mdat", mdat_payload)
    base = len(ftyp) + 8
    offsets, o = [], base
    for s in gps_samples:
        offsets.append(o)
        o += len(s)
    n = len(gps_samples)

    mvhd = full(b"mvhd", struct.pack(">IIII", ctime, ctime, 1000, int(duration * 1000)) + b"\0" * 80)
    mdhd = full(b"mdhd", struct.pack(">IIII", ctime, ctime, 1000, int(duration * 1000)) + b"\0" * 4)
    hdlr = full(b"hdlr", b"\0\0\0\0meta" + b"\0" * 12 + b"GoPro MET\0")
    stsd = full(b"stsd", struct.pack(">I", 1) + box(b"gpmd", b"\0" * 8))
    stts = full(b"stts", struct.pack(">III", 1, n, 1000))
    stsc = full(b"stsc", struct.pack(">IIII", 1, 1, 1, 1))
    stsz = full(b"stsz", struct.pack(">II", 0, n) + b"".join(struct.pack(">I", len(s)) for s in gps_samples))
    stco = full(b"stco", struct.pack(">I", n) + b"".join(struct.pack(">I", x) for x in offsets))
    stbl = box(b"stbl", stsd + stts + stsc + stsz + stco)
    trak = box(b"trak", box(b"mdia", mdhd + hdlr + box(b"minf", stbl))) if n else b""
    moov = box(b"moov", mvhd + (video_trak(*video) if video else b"") + trak)
    with open(path, "wb") as f:
        f.write(ftyp + mdat + moov)
