"""Tests for processor.py — extraction routing by MIME type and extension."""

import pytest

from processor import (
    EXT_EXTRACTORS, MIME_EXTRACTORS, _looks_garbled, extract_text_from_image,
)


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


# ─── _looks_garbled ─────────────────────────────────────────────────────


def test_looks_garbled_flags_positioned_glyph_output():
    """The actual garbled extract from the mobile-capture Sienna
    registration upload that triggered this whole investigation. Every
    other line is 1-2 characters because the upstream PDF was built from
    per-glyph drawing ops, not real text runs."""
    sample = """e l
L01579
Ic
b
r
a
e
Y
D
J R
K E
C 1 N
S 1 2 9
5 7 2
)
E
D
R"""
    assert _looks_garbled(sample) is True


def test_looks_garbled_passes_real_document_text():
    """A normal extraction from a real PDF text layer should not trigger
    a force-OCR retry — that's expensive and unnecessary."""
    sample = """AutoNation Toyota Arapahoe
Service Invoice 1961187
Vehicle: 2022 Toyota Sienna
Customer: David Coles
Total: $336.56
Payment: Cash
Date: 2026-05-15"""
    assert _looks_garbled(sample) is False


def test_looks_garbled_passes_when_text_is_short():
    """Don't fire on short legitimate snippets (a printed PIN, a sliver
    receipt) — the heuristic needs enough lines to be confident."""
    sample = "PIN: 1234\nValid 30 days"
    assert _looks_garbled(sample) is False


def test_looks_garbled_passes_short_lines_in_long_document():
    """Tables and forms have some single-word lines (Yes / No / N/A
    answers) but not 40%+. The threshold tolerates them."""
    sample = "\n".join([
        "Patient Name: Dave Coles",
        "DOB: 1985-04-12",
        "Sex: M",
        "Vital signs:",
        "BP: 120/80",
        "Yes",
        "No",
        "N/A",
        "",
        "Visit notes:",
        "Patient reports feeling well overall.",
        "Recommended annual follow-up.",
    ])
    assert _looks_garbled(sample) is False


def test_looks_garbled_handles_empty_input():
    assert _looks_garbled("") is False
    assert _looks_garbled("\n\n\n") is False
