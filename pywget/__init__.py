#!/usr/bin/env python3

# File: pywget/__init__.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: pywget - a robust, resumable, multi-connection wget alternative and library.
# License: MIT

"""pywget - a robust, resumable, multi-connection wget alternative and library."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union

from .__version__ import __version__
from .client import ServerClient
from .config import ServerConfig
from .core import Downloader, DownloadResult, Options
from .errors import (
    ChecksumError,
    DownloadCancelled,
    HTTPStatusError,
    InvalidURLError,
    NetworkError,
    PywgetError,
    ServerError,
    StorageError,
)
from .progress import ProgressBar, ProgressInfo
from .server import ServerHandle, run_server

__all__ = [
    "__version__",
    "download",
    "Downloader",
    "DownloadResult",
    "Options",
    "ProgressBar",
    "ProgressInfo",
    "ServerClient",
    "ServerConfig",
    "ServerHandle",
    "ServerError",
    "run_server",
    "PywgetError",
    "InvalidURLError",
    "NetworkError",
    "HTTPStatusError",
    "ChecksumError",
    "StorageError",
    "DownloadCancelled",
]


def download(url: str, path: Union[str, Path] = ".", *, progress: bool = False, **options: Any) -> Path:
    """Download *url* into directory *path*; return the final file path.

    Keyword options are those of :class:`Options` (``connections``, ``retries``,
    ``checksum``, ``limit_rate`` ...). ``progress=True`` shows a terminal bar.
    """
    bar = ProgressBar() if progress else None
    try:
        return Downloader(Options(output_dir=str(path), progress=bar, **options)).download(url)
    finally:
        if bar is not None:
            bar.close()
