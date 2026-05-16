"""Tests for helpers.py — file-type classification from MIME types."""

import pytest

from helpers import file_type_from_mime


@pytest.mark.parametrize("mime,expected", [
    ("application/pdf", "pdf"),
    ("image/png", "image"),
    ("image/jpeg", "image"),
    ("image/tiff", "image"),
    ("text/plain", "text"),
    ("text/csv", "text"),
    ("application/json", "text"),
    ("application/octet-stream", "other"),
    ("application/msword", "other"),
    ("", "other"),
])
def test_file_type_from_mime(mime, expected):
    assert file_type_from_mime(mime) == expected
