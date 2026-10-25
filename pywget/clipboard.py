"""Clipboard and URL-extraction helpers (stdlib only)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from typing import List

from .errors import PywgetError

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def extract_urls(text: str) -> List[str]:
    seen: List[str] = []
    for url in _URL_RE.findall(text or ""):
        url = url.rstrip(".,;)]}")
        if url not in seen:
            seen.append(url)
    return seen


def read_clipboard() -> str:
    if sys.platform == "win32":
        commands = [["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]]
    elif sys.platform == "darwin":
        commands = [["pbpaste"]]
    else:
        commands = [
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ]
    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            done = subprocess.run(command, capture_output=True, timeout=5, check=True)
            return done.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            continue
    raise PywgetError("cannot read the clipboard (no supported clipboard tool found)")
