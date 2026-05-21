"""Tests for processor.py — extraction routing by MIME type and extension."""

import pytest

from processor import EXT_EXTRACTORS, MIME_EXTRACTORS, extract_text_from_image


# Image formats that must route to OCR. HEIC/HEIF is the iPhone camera
# default; WEBP/GIF round out the common raster set the upload allowlist
# (config.allowed_upload_mime) already accepts.
@pytest.mark.parametrize("mime", [
    "image/png", "image/jpeg", "image/tiff", "image/bmp",
    "image/heic", "image/heif", "image/webp", "image/gif",
])
def test_image_mime_routes_to_ocr(mime):
    assert MIME_EXTRACTORS.get(mime) is extract_text_from_image


@pytest.mark.parametrize("ext", [
    ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp",
    ".heic", ".heif", ".webp", ".gif",
])
def test_image_ext_routes_to_ocr(ext):
    assert EXT_EXTRACTORS.get(ext) is extract_text_from_image


def test_no_extractor_for_unknown_type():
    assert MIME_EXTRACTORS.get("application/octet-stream") is None
    assert EXT_EXTRACTORS.get(".xyz") is None
