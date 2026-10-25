#!/usr/bin/env python3

# File: tests/test_utils.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: 
# License: MIT

import pytest

from pywget.errors import InvalidURLError
from pywget.utils import (
    RateLimiter,
    filename_from_content_disposition,
    format_duration,
    format_size,
    normalize_url,
    parse_checksum,
    parse_size,
    sanitize_filename,
    unique_path,
)


@pytest.mark.parametrize(
    "text,expected",
    [("500k", 512000), ("2M", 2 * 1024**2), ("1.5G", int(1.5 * 1024**3)), ("2MiB", 2 * 1024**2), ("100", 100)],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_size_invalid():
    with pytest.raises(ValueError):
        parse_size("fast")


def test_format():
    assert format_size(512) == "512 B"
    assert format_size(1536) == "1.5 KiB"
    assert format_duration(75) == "01:15"
    assert format_duration(None) == "--:--"


@pytest.mark.parametrize(
    "raw,safe",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\evil.exe", "evil.exe"),
        ("a:b*c?.txt", "a_b_c_.txt"),
        ("", "index.html"),
        ("..", "index.html"),
        ("CON.txt", "_CON.txt"),
        (".bashrc", ".bashrc"),
    ],
)
def test_sanitize(raw, safe):
    assert sanitize_filename(raw) == safe


def test_content_disposition():
    assert filename_from_content_disposition('attachment; filename="a b.zip"') == "a b.zip"
    assert filename_from_content_disposition("attachment; filename*=UTF-8''na%C3%AFve.txt") == "naïve.txt"
    assert filename_from_content_disposition(None) is None


def test_checksum_parse():
    assert parse_checksum("sha256:" + "ab" * 32) == ("sha256", "ab" * 32)
    assert parse_checksum("cd" * 16)[0] == "md5"
    with pytest.raises(ValueError):
        parse_checksum("nope:zz")


def test_normalize_url():
    url, user, pw = normalize_url("https://u:p%40ss@Example.com/a b?q=1#frag")
    assert url == "https://example.com/a%20b?q=1"
    assert (user, pw) == ("u", "p@ss")
    for bad in ("ftp://x/y", "file:///etc/passwd", "http://", "nonsense"):
        with pytest.raises(InvalidURLError):
            normalize_url(bad)


def test_unique_path(tmp_path):
    target = tmp_path / "f.bin"
    assert unique_path(target) == target
    target.write_text("x")
    assert unique_path(target) == tmp_path / "f.bin.1"


def test_rate_limiter_unlimited_is_instant():
    RateLimiter(0).acquire(10**9)
