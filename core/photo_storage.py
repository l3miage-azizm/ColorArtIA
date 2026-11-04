"""Keep JPEG pixels compressed while removing photographic metadata.

This edits JPEG marker segments only. Quantization tables, scan headers and
entropy-coded image data are copied byte for byte; JPEG is never re-encoded.
Unsupported or malformed marker layouts return None so callers can use their
existing normalized PNG fallback.
"""
from __future__ import annotations

from PIL import Image


def sanitized_jpeg(content: bytes, orientation: int = 1) -> bytes | None:
    if (not isinstance(content, bytes) or not content.startswith(b"\xff\xd8")
            or type(orientation) is not int or orientation not in range(1, 9)):
        return None
    result = bytearray(b"\xff\xd8")
    if orientation != 1:
        exif = Image.Exif()
        exif[274] = orientation
        payload = exif.tobytes()
        result.extend(b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload)
    position, frame, scan = 2, False, False
    size = len(content)
    try:
        while position < size:
            start = position
            if content[position] != 0xFF:
                return None
            while position < size and content[position] == 0xFF:
                position += 1
            if position == size:
                return None
            marker = content[position]
            position += 1
            if marker in {0, 0xD8}:
                return None
            if marker == 0xD9:
                if not frame or not scan:
                    return None
                result.extend(content[start:position])
                return bytes(result)  # Discard any data after the JPEG end.
            if marker == 1 or 0xD0 <= marker <= 0xD7:
                result.extend(content[start:position])
                continue
            if position + 2 > size:
                return None
            length = int.from_bytes(content[position:position + 2], "big")
            end = position + length
            if length < 2 or end > size:
                return None
            payload = content[position + 2:end]
            if marker in {0xC0, 0xC1, 0xC2}:
                frame = True
            elif marker in {0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                return None
            if marker == 0xE0:
                if payload.startswith(b"JFIF\x00"):
                    if len(payload) < 14:
                        return None
                    # Keep density/version flags, remove embedded thumbnails.
                    payload = payload[:12] + b"\x00\x00"
                    result.extend(b"\xff\xe0" + (len(payload) + 2).to_bytes(2, "big") + payload)
            elif marker == 0xEE:
                if not payload.startswith(b"Adobe") or len(payload) < 12:
                    return None
                # Adobe's color-transform flag is required for CMYK/YCCK.
                result.extend(b"\xff\xee\x00\x0e" + payload[:12])
            elif not (0xE0 <= marker <= 0xEF or marker == 0xFE):
                result.extend(content[start:end])
            position = end
            if marker != 0xDA:
                continue
            if not frame:
                return None
            scan = True
            entropy_start = position
            while True:
                boundary = content.find(b"\xff", position)
                if boundary < 0:
                    return None
                following = boundary + 1
                while following < size and content[following] == 0xFF:
                    following += 1
                if following == size:
                    return None
                code = content[following]
                if code == 0 or 0xD0 <= code <= 0xD7:
                    position = following + 1
                    continue
                # Parse the next marker normally, including APP/COM segments
                # between progressive scans. Never copy post-scan EXIF blindly.
                result.extend(content[entropy_start:boundary])
                position = boundary
                break
    except (IndexError, OverflowError, ValueError):
        return None
    return None
