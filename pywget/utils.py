#!/usr/bin/env python3

# File: pywget/utils.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: Pure helpers: parsing, formatting, filenames, hashing, rate limiting.
# License: MIT

"""Pure helpers: parsing, formatting, filenames, hashing, rate limiting."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import unicodedata
from email.message import Message
from pathlib import Path
from typing import Optional, Tuple, Union
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .errors import InvalidURLError

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(i?b)?\s*$", re.I)
_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)
}
_HEX = re.compile(r"^[0-9a-f]+$")
_GUESS_ALGO = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}


def parse_size(value: Union[str, int, float]) -> int:
    """Parse ``500k`` / ``2MiB`` / ``1.5G`` into bytes."""
    if isinstance(value, (int, float)):
        return int(value)
    match = _SIZE_RE.match(str(value))
    if not match:
        raise ValueError(f"invalid size: {value!r}")
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def format_size(num: float) -> str:
    num = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{int(num)} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TiB"  # pragma: no cover


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds != seconds or seconds < 0 or seconds > 99 * 3600:
        return "--:--"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def sanitize_filename(name: Optional[str], default: str = "index.html") -> str:
    """Return a safe single path component (no traversal, no reserved names)."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _BAD_CHARS.sub("_", name).strip().rstrip(" .")
    if not name:
        return default
    if name.split(".")[0].lower() in _RESERVED:
        name = "_" + name
    if len(name.encode("utf-8")) > 200:
        root, ext = os.path.splitext(name)
        ext = ext[:20]
        budget = 200 - len(ext.encode("utf-8"))
        name = root.encode("utf-8")[:budget].decode("utf-8", "ignore") + ext
    return name


def filename_from_content_disposition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    msg = Message()
    msg["Content-Disposition"] = value
    try:
        filename = msg.get_filename()
    except Exception:  # malformed header
        return None
    return filename or None


def filename_from_url(url: str) -> Optional[str]:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or None


def unique_path(path: Path) -> Path:
    """``file.txt`` -> ``file.txt.1`` -> ``file.txt.2`` (wget style)."""
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.name}.{index}")
        if not candidate.exists() and not Path(f"{candidate}.part").exists():
            return candidate
    raise FileExistsError(f"cannot find a free name for {path}")


def parse_checksum(spec: str) -> Tuple[str, str]:
    """``sha256:<hex>`` or a bare hex digest (algorithm guessed by length)."""
    algo, sep, digest = spec.strip().partition(":")
    if not sep:
        digest = algo
        guessed = _GUESS_ALGO.get(len(digest))
        if not guessed:
            raise ValueError("cannot guess hash algorithm; use ALGO:HEX")
        algo = guessed
    algo = algo.lower().replace("-", "")
    digest = digest.strip().lower()
    if algo not in hashlib.algorithms_guaranteed or algo.startswith("shake"):
        raise ValueError(f"unsupported hash algorithm: {algo}")
    if not digest or not _HEX.match(digest):
        raise ValueError("checksum digest must be hexadecimal")
    return algo, digest


def hash_file(path: Union[str, Path], algo: str) -> str:
    digest = hashlib.new(algo)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_url(url: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Validate an http(s) URL; return (clean_url, username, password)."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError(f"invalid URL {url!r}: {exc}") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise InvalidURLError(f"unsupported or malformed URL: {url!r} (only http/https)")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidURLError(f"invalid host name in URL: {url!r}") from None
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=~")
    query = quote(parts.query, safe="=&%+/?:@!$'()*,;~[]")
    clean = urlunsplit((parts.scheme.lower(), netloc, path, query, ""))
    user = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else None
    return clean, user, password


class RateLimiter:
    """Thread-safe global bandwidth limiter (bytes/second, 0 = unlimited)."""

    def __init__(self, rate: int = 0) -> None:
        self.rate = max(0, int(rate))
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self, nbytes: int, cancel: Optional[threading.Event] = None) -> None:
        if not self.rate:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + nbytes / self.rate
        delay = start - now
        if delay > 0.001:
            if cancel is not None:
                cancel.wait(delay)
            else:
                time.sleep(delay)
