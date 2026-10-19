"""Draw the app icon (blue rounded square + white upload arrow) as a multi-size .ico.

Deliberately not red + play triangle, which would look like YouTube's logo.
Pure Python so building needs no Pillow. Run: python packaging/make_icon.py
"""
import os
import struct
import zlib

SIZES = (16, 24, 32, 48, 64, 128, 256)
BLUE = (24, 108, 196)
WHITE = (255, 255, 255)


def _coverage(x, y, n, inside):
    """Fraction of a pixel inside a shape, 4x4 supersampled for smooth edges."""
    hits = 0
    for sy in range(4):
        for sx in range(4):
            if inside((x + (sx + 0.5) / 4) / n, (y + (sy + 0.5) / 4) / n):
                hits += 1
    return hits / 16


def _in_rounded_square(u, v, r=0.22):
    cu, cv = min(max(u, r), 1 - r), min(max(v, r), 1 - r)
    return (u - cu) ** 2 + (v - cv) ** 2 <= r * r


def _in_arrow(u, v):
    # upload arrow: head (triangle pointing up), shaft, and a tray line below
    if 0.20 <= v <= 0.48 and abs(u - 0.5) <= (v - 0.20) / 0.28 * 0.24:
        return True
    if 0.47 <= v <= 0.68 and abs(u - 0.5) <= 0.085:
        return True
    return 0.75 <= v <= 0.82 and 0.24 <= u <= 0.76


def _png(n):
    rows = []
    for y in range(n):
        row = bytearray([0])
        for x in range(n):
            bg = _coverage(x, y, n, _in_rounded_square)
            fg = _coverage(x, y, n, _in_arrow)
            rgb = [round(BLUE[i] * (1 - fg) + WHITE[i] * fg) for i in range(3)]
            row += bytes(rgb + [round(255 * bg)])
        rows.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


def main(out=os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")):
    images = [_png(n) for n in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for n, img in zip(SIZES, images):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
        blobs += img
    with open(out, "wb") as f:
        f.write(header + entries + blobs)
    print("wrote", out)


if __name__ == "__main__":
    main()
