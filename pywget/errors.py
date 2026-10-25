#!/usr/bin/env python3

# File: pywget/errors.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: Exception hierarchy. Every exception carries a CLI ``exit_code``.
# License: MIT

"""Exception hierarchy. Every exception carries a CLI ``exit_code``."""

from __future__ import annotations

from typing import Optional

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class PywgetError(Exception):
    """Base class for all pywget errors."""

    exit_code = 1


class InvalidURLError(PywgetError):
    exit_code = 2


class NetworkError(PywgetError):
    """Transport-level failure. ``retryable`` tells the engine whether to retry."""

    exit_code = 3

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after: Optional[float] = None


class HTTPStatusError(NetworkError):
    exit_code = 4

    def __init__(
        self,
        status: int,
        url: str,
        reason: str = "",
        retry_after: Optional[float] = None,
    ) -> None:
        from .utils import short_url  # lazy: utils imports this module

        text = f"HTTP {status} {reason}".strip()
        super().__init__(f"{text} for {short_url(url)}", retryable=status in RETRY_STATUS)
        self.status = status
        self.url = url
        self.retry_after = retry_after


class ChecksumError(PywgetError):
    exit_code = 5


class StorageError(PywgetError):
    exit_code = 6


class DownloadCancelled(PywgetError):
    exit_code = 130


class ServerError(PywgetError):
    """Error talking to (or reported by) a pywget server/agent."""

    exit_code = 7

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ConfigError(PywgetError):
    """Invalid configuration file or token."""

    exit_code = 2
