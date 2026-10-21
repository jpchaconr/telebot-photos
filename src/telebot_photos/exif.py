"""Write who sent a photo and its caption into the JPEG's EXIF, without re-encoding it."""

from __future__ import annotations

import struct
from pathlib import Path

EXIF_HEADER = b"Exif\x00\x00"
SOI = b"\xff\xd8"
APP0, APP1, SOS = 0xE0, 0xE1, 0xDA

# IFD0 tags. EXIF "ASCII" strings are written as UTF-8, as the Metadata Working Group
# recommends; the XP* tags are UTF-16 and are what Windows shows as Comments and Authors.
IMAGE_DESCRIPTION = 0x010E
ARTIST = 0x013B
XP_COMMENT = 0x9C9C
XP_AUTHOR = 0x9C9D
TYPE_BYTE, TYPE_ASCII = 1, 2

# Keep the segment well under the 64 KB a JPEG segment can hold, even with 4-byte characters.
MAX_DESCRIPTION = 4000
MAX_ARTIST = 300


def build_exif(description: str | None, artist: str | None) -> bytes:
    """The payload of an APP1 segment: ``Exif\\0\\0`` and a little-endian TIFF with IFD0."""
    entries: list[tuple[int, int, bytes]] = []
    if description:
        description = description[:MAX_DESCRIPTION]
        entries.append((IMAGE_DESCRIPTION, TYPE_ASCII, description.encode() + b"\0"))
        entries.append((XP_COMMENT, TYPE_BYTE, description.encode("utf-16-le") + b"\0\0"))
    if artist:
        artist = artist[:MAX_ARTIST]
        entries.append((ARTIST, TYPE_ASCII, artist.encode() + b"\0"))
        entries.append((XP_AUTHOR, TYPE_BYTE, artist.encode("utf-16-le") + b"\0\0"))
    entries.sort()  # IFD entries must be in ascending tag order

    ifd = struct.pack("<H", len(entries))
    data = b""
    data_offset = 8 + 2 + 12 * len(entries) + 4  # values follow the TIFF header and the IFD
    for tag, kind, value in entries:
        if len(value) <= 4:
            ifd += struct.pack("<HHI", tag, kind, len(value)) + value.ljust(4, b"\0")
        else:
            ifd += struct.pack("<HHII", tag, kind, len(value), data_offset + len(data))
            data += value + b"\0" * (len(value) % 2)  # values start on a word boundary
    ifd += struct.pack("<I", 0)  # no IFD1
    return EXIF_HEADER + b"II*\0" + struct.pack("<I", 8) + ifd + data


def add_exif(jpeg: bytes, *, description: str | None, artist: str | None) -> bytes | None:
    """Return the JPEG with the tags, or None if it is not a JPEG or already has EXIF.

    The new segment goes right after SOI and any JFIF (APP0) segment; the image data is
    copied byte for byte.
    """
    if not jpeg.startswith(SOI) or not (description or artist):
        return None
    insert_at = pos = len(SOI)
    while pos + 4 <= len(jpeg):
        if jpeg[pos] != 0xFF:
            return None
        marker = jpeg[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == SOS:  # the image data starts: no more metadata segments
            break
        length = int.from_bytes(jpeg[pos + 2 : pos + 4], "big")
        if length < 2:
            return None
        if marker == APP1 and jpeg[pos + 4 : pos + 10] == EXIF_HEADER:
            return None  # never overwrite EXIF the photo already has
        start, pos = pos, pos + 2 + length
        if marker == APP0 and insert_at == start:
            insert_at = pos
    payload = build_exif(description, artist)
    segment = bytes([0xFF, APP1]) + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:insert_at] + segment + jpeg[insert_at:]


def write_tagged_copy(
    source: Path, target: Path, *, description: str | None, artist: str | None
) -> int | None:
    """Write ``source`` with the tags to ``target`` and return its size (None: not tagged)."""
    tagged = add_exif(source.read_bytes(), description=description, artist=artist)
    if tagged is None:
        return None
    target.write_bytes(tagged)
    return len(tagged)
