#!/usr/bin/env python3

# File: pywget/progress.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: Progress tracking and terminal rendering.
# License: MIT

"""Progress tracking and terminal rendering."""

from __future__ import annotations

import collections
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from typing import Deque, Optional, TextIO, Tuple

from .utils import format_duration, format_size


@dataclass(frozen=True)
class ProgressInfo:
    url: str
    filename: str
    downloaded: int
    total: Optional[int]
    speed: float
    eta: Optional[float]
    elapsed: float
    finished: bool = False

    @property
    def percent(self) -> Optional[float]:
        if self.total is None:
            return None
        if self.total == 0:
            return 100.0
        return min(100.0, self.downloaded * 100.0 / self.total)


class Tracker:
    """Thread-safe byte counter producing speed/ETA snapshots."""

    WINDOW = 5.0

    def __init__(self, url: str, filename: str, total: Optional[int]) -> None:
        self.url = url
        self.filename = filename
        self.total = total
        self._lock = threading.Lock()
        self._downloaded = 0
        self._start = time.monotonic()
        self._samples: Deque[Tuple[float, int]] = collections.deque()
        self._samples.append((self._start, 0))

    def add(self, nbytes: int) -> None:
        with self._lock:
            self._downloaded += nbytes

    def set(self, nbytes: int) -> None:
        with self._lock:
            self._downloaded = nbytes
            self._samples.clear()
            self._samples.append((time.monotonic(), nbytes))

    def reset(self) -> None:
        self.set(0)

    @property
    def downloaded(self) -> int:
        with self._lock:
            return self._downloaded

    def snapshot(self, finished: bool = False) -> ProgressInfo:
        now = time.monotonic()
        with self._lock:
            done = self._downloaded
            self._samples.append((now, done))
            while len(self._samples) > 2 and now - self._samples[0][0] > self.WINDOW:
                self._samples.popleft()
            t0, b0 = self._samples[0]
        speed = max((done - b0) / (now - t0), 0.0) if now > t0 else 0.0
        eta = None
        if self.total is not None and speed > 0:
            eta = max(self.total - done, 0) / speed
        return ProgressInfo(
            self.url, self.filename, done, self.total, speed, eta, now - self._start, finished
        )


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 2, 1)] + ".."


class ProgressBar:
    """Single-line ASCII progress bar; silent when the stream is not a TTY."""

    def __init__(
        self,
        stream: Optional[TextIO] = None,
        enabled: bool = True,
        min_interval: float = 0.1,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.min_interval = min_interval
        isatty = getattr(self.stream, "isatty", None)
        self._tty = bool(isatty and isatty())
        self._last = 0.0
        self._last_len = 0
        self._dirty = False

    def __call__(self, info: ProgressInfo) -> None:
        self.update(info)

    def update(self, info: ProgressInfo) -> None:
        if not (self.enabled and self._tty):
            return
        now = time.monotonic()
        if not info.finished and now - self._last < self.min_interval:
            return
        self._last = now
        line = self._format(info)
        pad = " " * max(0, self._last_len - len(line))
        self.stream.write("\r" + line + pad)
        self.stream.flush()
        self._last_len = len(line)
        self._dirty = True
        if info.finished:
            self.close()

    def close(self) -> None:
        if self._dirty:
            self.stream.write("\n")
            self.stream.flush()
        self._dirty = False
        self._last_len = 0

    @staticmethod
    def _format(info: ProgressInfo) -> str:
        cols = shutil.get_terminal_size((80, 24)).columns - 1
        pct = f"{info.percent:5.1f}%" if info.percent is not None else "   --%"
        size = format_size(info.downloaded)
        if info.total is not None:
            size += f"/{format_size(info.total)}"
        tail = f"{pct} {size} {format_size(info.speed)}/s ETA {format_duration(info.eta)}"
        name_w = max(8, min(28, cols // 4))
        name = _truncate(info.filename, name_w).ljust(name_w)
        room = cols - len(name) - len(tail) - 4
        if info.percent is not None and room >= 12:
            inner_w = room - 2
            filled = int(inner_w * info.percent / 100)
            inner = ("=" * filled + (">" if filled < inner_w else "")).ljust(inner_w)[:inner_w]
            return f"{name} [{inner}] {tail}"
        return f"{name} {tail}"[:cols]
