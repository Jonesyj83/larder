"""Draws the png app icons at startup so there's no binary files to keep in the repo.
Same idea as icon.svg, just blockier."""
import struct
import zlib

BACKGROUND = (23, 29, 37)
JAR = (242, 244, 247)
LID = (186, 196, 209)
LABEL = (233, 180, 71)


def _pixel(x, y):
    # coordinates in a 512 box
    if 166 <= x < 346 and 96 <= y < 144:
        return LID
    if 120 <= x < 392 and 168 <= y < 440:
        if 150 <= x < 362 and 250 <= y < 340:
            return LABEL
        return JAR
    return BACKGROUND


def make_png(size):
    raw = bytearray()
    scale = 512 / size
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(_pixel(int(x * scale), int(y * scale)))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
