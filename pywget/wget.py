#!/usr/bin/env python3

# File: pywget/wget.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: Backward-compatible API: ``from pywget import wget; wget.download(url, dir)``.
# License: MIT

"""Backward-compatible API: ``from pywget import wget; wget.download(url, dir)``."""

from __future__ import annotations

import sys
from typing import Any, List, Optional

from .core import Downloader, Options
from .progress import ProgressBar


def download(
    url: str,
    path: Optional[str] = None,
    filename: Optional[str] = None,
    bar: bool = True,
    **options: Any,
) -> str:
    """Download *url* into directory *path* and return the saved file path as ``str``."""
    progress = ProgressBar() if bar else None
    opts = Options(output_dir=path or ".", progress=progress, **options)
    try:
        return str(Downloader(opts).download(url, filename=filename))
    finally:
        if progress is not None:
            progress.close()


def usage(argv: Optional[List[str]] = None) -> int:
    """Legacy console entry point (kept so old installs/scripts keep working)."""
    from .cli import main

    return main(argv if argv is not None else sys.argv[1:])
