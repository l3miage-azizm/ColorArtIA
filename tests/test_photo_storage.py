from io import BytesIO
from pathlib import Path
import struct

import numpy as np
from PIL import Image, ImageOps
import pytest

from core.photo_storage import sanitized_jpeg
from core.library import Library, ValidationError


def encoded(*, orientation=1, progressive=False, mode="RGB"):
    channels = 4 if mode == "CMYK" else 3
    pixels = np.random.default_rng(71).integers(0, 256, (81, 113, channels), dtype=np.uint8)
    image = Image.fromarray(pixels, mode=mode)
    exif = Image.Exif()
    exif[274] = orientation
    exif[270] = "PRIVATE IMAGE DESCRIPTION"
    exif[34853] = {1: "N", 2: (40.0, 20.0, 10.0)}
    output = BytesIO()
    image.save(output, format="JPEG", quality=93, progressive=progressive, exif=exif)
    data = output.getvalue()
    # Other metadata must disappear as well as EXIF, even between scans.
    comment = b"PRIVATE GPS COMMENT"
    segment = b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    xmp = b"http://ns.adobe.com/xap/1.0/\x00PRIVATE XMP GPS"
    app1 = b"\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp
    data = data[:2] + segment + app1 + data[2:]
    return data


def pixels(data):
    with Image.open(BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        return np.array(image)


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("progressive", [False, True])
@pytest.mark.parametrize("mode", ["RGB", "CMYK"])
def test_jpeg_pixels_and_orientation_are_exact_without_private_metadata(orientation, progressive, mode):
    original = encoded(orientation=orientation, progressive=progressive, mode=mode)
    stored = sanitized_jpeg(original, orientation)
    assert stored is not None
    assert np.array_equal(pixels(original), pixels(stored))
    assert b"PRIVATE" not in stored
    with Image.open(BytesIO(stored)) as image:
        assert set(image.getexif()) <= {274}
        assert image.getexif().get(274, 1) == orientation
        assert 34853 not in image.getexif()
    assert len(stored) < len(original)


def test_metadata_between_progressive_scans_and_after_last_scan_is_removed():
    original = encoded(progressive=True, orientation=6)
    text = b"PRIVATE AFTER SCAN GPS"
    metadata = b"\xff\xe1" + struct.pack(">H", len(text) + 2) + text
    # Locate a marker after the first SOS; a DHT immediately before a later
    # progressive scan provides a legal metadata insertion boundary.
    scan = original.index(b"\xff\xda")
    boundary = original.find(b"\xff\xc4", scan + 2)
    assert boundary > scan
    original = original[:boundary] + metadata + original[boundary:]
    original = original[:-2] + metadata + original[-2:] + b"PRIVATE TRAILING GPS"
    stored = sanitized_jpeg(original, 6)
    assert stored is not None and b"PRIVATE" not in stored
    assert stored.endswith(b"\xff\xd9")
    assert np.array_equal(pixels(original), pixels(stored))


@pytest.mark.parametrize("data", [b"", b"bad", b"\xff\xd8", b"\xff\xd8\xff\xe1\x00\x01", b"\xff\xd8\xff\xe1\xff\xff", b"\xff\xd8\xff\xd9"])
def test_malformed_or_unsupported_data_falls_back(data):
    assert sanitized_jpeg(data) is None


def test_unsupported_frame_and_orientation_fall_back():
    data = encoded()
    assert sanitized_jpeg(data, 19) is None
    assert sanitized_jpeg(data.replace(b"\xff\xc0", b"\xff\xc3", 1)) is None


def test_library_stores_jpeg_losslessly_and_keeps_pixel_duplicate_detection(tmp_path):
    library = Library(tmp_path / "library")
    book = library.create_book("Photos", target_photos=2)
    content = encoded(orientation=8, progressive=True)
    page = library.add_photo(book["id"], content, "phone.jpg")
    assert page["source_path"].endswith("source.jpg")
    stored = library.safe_asset(book["id"], page["source_path"]).read_bytes()
    assert np.array_equal(pixels(stored), pixels(content))
    assert (page["width"], page["height"]) == (81, 113)
    assert len(stored) <= len(content)
    assert library.safe_asset(book["id"], page["thumbnail_path"]).is_file()
    with pytest.raises(ValidationError, match="déjà présente"):
        library.add_photo(book["id"], stored, "renamed.jpg")


def test_png_and_unsupported_jpeg_marker_layout_retain_existing_png_fallback(tmp_path, monkeypatch):
    library = Library(tmp_path / "library")
    book = library.create_book("Photos", target_photos=2)
    output = BytesIO()
    Image.new("RGB", (40, 60), "red").save(output, format="PNG")
    png = library.add_photo(book["id"], output.getvalue(), "photo.png")
    assert png["source_path"].endswith("source.png")
    monkeypatch.setattr("core.photo_storage.sanitized_jpeg", lambda *args: None)
    jpeg = library.add_photo(book["id"], encoded(), "phone.jpg")
    assert jpeg["source_path"].endswith("source.png")
    saved = library.safe_asset(book["id"], jpeg["source_path"]).read_bytes()
    assert np.array_equal(pixels(saved), pixels(encoded()))


def test_upload_size_and_pixel_limits_still_apply(tmp_path, monkeypatch):
    library = Library(tmp_path / "library")
    book = library.create_book("Photos", target_photos=1)
    monkeypatch.setattr("core.library.MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ValidationError, match="20 Mio"):
        library.add_photo(book["id"], encoded())
    monkeypatch.setattr("core.library.MAX_UPLOAD_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr("core.library.MAX_IMAGE_PIXELS", 10)
    with pytest.raises(ValidationError, match="30 millions"):
        library.add_photo(book["id"], encoded())
