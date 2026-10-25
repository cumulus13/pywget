#!/usr/bin/env python3

# File: pywget/core.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: Download engine: probing, segmented/resumable transfer, retries, verification.
# License: MIT

"""Download engine: probing, segmented/resumable transfer, retries, verification."""

from __future__ import annotations

import base64
import dataclasses
import errno
import functools
import http.client
import json
import logging
import os
import random
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .__version__ import __version__
from .errors import (
    ChecksumError,
    DownloadCancelled,
    HTTPStatusError,
    NetworkError,
    PywgetError,
    StorageError,
)
from .progress import ProgressInfo, Tracker
from .utils import (
    RateLimiter,
    filename_from_content_disposition,
    filename_from_url,
    format_size,
    hash_file,
    normalize_url,
    parse_checksum,
    parse_size,
    sanitize_filename,
    unique_path,
)

log = logging.getLogger("pywget")

_MIB = 1024 * 1024
_STATE_VERSION = 1
_NETWORK_EXC = (
    TimeoutError,
    socket.timeout,
    ConnectionError,
    http.client.HTTPException,
    ssl.SSLError,
    urllib.error.URLError,
    EOFError,
)
_FATAL_ERRNO = {
    getattr(errno, name)
    for name in ("ENOSPC", "EACCES", "EROFS", "ENOENT", "EISDIR", "ENOTDIR", "EDQUOT", "EPERM")
    if hasattr(errno, name)
}


@dataclass
class Options:
    """All tunables of a :class:`Downloader`."""

    output_dir: str = "."
    filename: Optional[str] = None
    connections: int = 4
    min_segment_size: int = 2 * _MIB
    retries: int = 5
    backoff: float = 1.0
    max_backoff: float = 30.0
    timeout: float = 30.0
    limit_rate: Any = 0  # bytes/s; accepts "500k", "2M"
    resume: bool = True
    overwrite: bool = False
    preserve_mtime: bool = True
    headers: Dict[str, str] = field(default_factory=dict)
    user_agent: str = f"pywget/{__version__}"
    username: Optional[str] = None
    password: Optional[str] = None
    proxy: Optional[str] = None
    verify_ssl: bool = True
    ca_file: Optional[str] = None
    checksum: Optional[str] = None  # "sha256:<hex>" or bare hex digest
    chunk_size: int = 64 * 1024
    progress: Optional[Callable[[ProgressInfo], None]] = None

    def __post_init__(self) -> None:
        if not 1 <= int(self.connections) <= 32:
            raise ValueError("connections must be between 1 and 32")
        if self.retries < 0:
            raise ValueError("retries must be >= 0")
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0")
        if self.chunk_size < 1024:
            raise ValueError("chunk_size must be >= 1024")
        if self.min_segment_size < 1:
            raise ValueError("min_segment_size must be >= 1")
        self.limit_rate = parse_size(self.limit_rate) if self.limit_rate else 0
        if self.checksum:
            parse_checksum(self.checksum)


@dataclass
class RemoteInfo:
    url: str
    size: Optional[int]
    accept_ranges: bool
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    filename: Optional[str] = None


@dataclass
class DownloadResult:
    url: str
    path: Path
    size: int
    elapsed: float
    connections: int = 1
    resumed: bool = False
    skipped: bool = False
    checksum: Optional[str] = None

    @property
    def speed(self) -> float:
        return self.size / self.elapsed if self.elapsed > 0 and not self.skipped else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "path": str(self.path),
            "size": self.size,
            "elapsed": round(self.elapsed, 3),
            "connections": self.connections,
            "resumed": self.resumed,
            "skipped": self.skipped,
            "checksum": self.checksum,
        }


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow only http(s) redirects; never leak credentials across origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urlsplit(newurl)
        if target.scheme.lower() not in ("http", "https"):
            raise urllib.error.HTTPError(
                req.full_url, code, "redirect to unsupported scheme blocked", headers, fp
            )
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old = urlsplit(req.full_url)
            if (old.scheme, old.hostname, old.port) != (target.scheme, target.hostname, target.port):
                for name in ("Authorization", "Cookie", "Proxy-authorization"):
                    new.headers.pop(name, None)
                    new.unredirected_hdrs.pop(name, None)
        return new


class _Worker(threading.Thread):
    def __init__(self, target: Callable[[], None], name: str) -> None:
        super().__init__(name=name, daemon=True)
        self._target_fn = target
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            self._target_fn()
        except BaseException as exc:  # noqa: BLE001 - propagated by the monitor
            self.error = exc


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, NetworkError):
        return exc.retryable
    if isinstance(exc, PywgetError):
        return False
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, OSError):
        if exc.errno in _FATAL_ERRNO:
            return False
        return not isinstance(
            exc, (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError)
        )
    return isinstance(exc, (http.client.HTTPException, EOFError))


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return min(float(value), 120.0)
    except ValueError:
        pass
    try:
        delta = parsedate_to_datetime(value).timestamp() - time.time()
        return min(max(delta, 0.0), 120.0)
    except (TypeError, ValueError, OverflowError):
        return None


def _write_all(handle: Any, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = handle.write(view)
        view = view[written:]


def _unlink(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class Downloader:
    """Resumable, multi-connection HTTP(S) downloader.

    A ``Downloader`` runs one download at a time; create one per thread.
    """

    def __init__(self, options: Optional[Options] = None, **overrides: Any) -> None:
        opts = options or Options()
        if overrides:
            opts = dataclasses.replace(opts, **overrides)
        self.options = opts
        self._cancel = threading.Event()
        self._auth: Optional[Tuple[str, str]] = None
        self._last_report = 0.0
        self._tracker: Optional[Tracker] = None
        self._opener = self._build_opener()

    # ------------------------------------------------------------------ public

    def cancel(self) -> None:
        """Ask a running download to stop (state is kept for resuming)."""
        self._cancel.set()

    def download(
        self, url: str, output_dir: Optional[str] = None, filename: Optional[str] = None
    ) -> Path:
        return self.fetch(url, output_dir=output_dir, filename=filename).path

    def fetch(
        self, url: str, output_dir: Optional[str] = None, filename: Optional[str] = None
    ) -> DownloadResult:
        o = self.options
        started = time.monotonic()
        clean_url, user, password = normalize_url(url)
        self._auth = (user, password or "") if user is not None else None
        self._cancel.clear()
        self._last_report = 0.0

        info = self._with_retries(lambda: self._probe(clean_url), "probe")
        explicit = filename or o.filename
        name = sanitize_filename(
            explicit
            or info.filename
            or filename_from_url(info.url)
            or filename_from_url(clean_url)
        )
        dest_dir = Path(output_dir if output_dir is not None else o.output_dir).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        part = dest.with_name(dest.name + ".part")
        state = dest.with_name(dest.name + ".pywget")

        if dest.exists() and not o.overwrite:
            if info.size is not None and dest.stat().st_size == info.size and not part.exists():
                digest = self._verify(dest)
                log.info("already complete: %s", dest)
                return DownloadResult(
                    clean_url, dest, info.size, time.monotonic() - started,
                    skipped=True, checksum=digest,
                )
            dest = unique_path(dest)
            part = dest.with_name(dest.name + ".part")
            state = dest.with_name(dest.name + ".pywget")
        elif o.overwrite:
            _unlink(part, state)

        tracker = Tracker(clean_url, dest.name, info.size)
        self._tracker = tracker
        limiter = RateLimiter(o.limit_rate)
        resumed = False
        used = 1

        if info.size == 0:
            part.write_bytes(b"")
        elif info.size is not None and info.accept_ranges:
            used = max(1, min(o.connections, info.size // o.min_segment_size))
            resumed, used = self._run_segmented(clean_url, info, part, state, tracker, limiter, used)
        else:
            if info.size is not None and o.connections > 1:
                log.debug("server has no Range support: single connection, no resume")
            _unlink(state)
            if info.size is not None:
                self._check_space(dest_dir, info.size)
            self._run_stream(clean_url, info, part, tracker, limiter)

        final_size = part.stat().st_size
        if info.size is not None and final_size != info.size:
            _unlink(part, state)
            raise NetworkError(f"size mismatch: expected {info.size}, got {final_size}")
        self._fsync(part)
        try:
            digest = self._verify(part)
        except ChecksumError:
            _unlink(part, state)
            raise
        os.replace(part, dest)
        _unlink(state)
        self._apply_mtime(dest, info.last_modified)
        self._report(tracker, finished=True)
        elapsed = time.monotonic() - started
        log.info("saved %s (%s in %.1fs)", dest, format_size(final_size), elapsed)
        return DownloadResult(
            clean_url, dest, final_size, elapsed, connections=used, resumed=resumed, checksum=digest
        )

    # ------------------------------------------------------------- HTTP layer

    def _build_opener(self) -> urllib.request.OpenerDirector:
        o = self.options
        if o.verify_ssl:
            ctx = ssl.create_default_context(cafile=o.ca_file)
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        handlers: List[Any] = [_SafeRedirectHandler()]
        if o.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": o.proxy, "https": o.proxy}))
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        o = self.options
        headers = {"User-Agent": o.user_agent, "Accept": "*/*", "Accept-Encoding": "identity"}
        user, pw = self._auth if self._auth else (o.username, o.password)
        if user is not None:
            token = base64.b64encode(f"{user}:{pw or ''}".encode()).decode("ascii")
            headers["Authorization"] = "Basic " + token
        headers.update(o.headers)
        if extra:
            headers.update(extra)
        return headers

    def _open(self, url: str, extra: Optional[Dict[str, str]] = None) -> Any:
        request = urllib.request.Request(url, headers=self._headers(extra))
        try:
            return self._opener.open(request, timeout=self.options.timeout)
        except urllib.error.HTTPError as exc:
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            reason = exc.reason if isinstance(exc.reason, str) else ""
            exc.close()
            raise HTTPStatusError(exc.code, url, reason, retry_after) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise NetworkError(
                    f"TLS certificate verification failed for {url}: {exc.reason} "
                    "(use --no-check-certificate or --ca-cert)",
                    retryable=False,
                ) from None
            raise NetworkError(f"{url}: {exc.reason}") from None

    def _probe(self, url: str) -> RemoteInfo:
        try:
            response = self._open(url, {"Range": "bytes=0-0"})
        except HTTPStatusError as exc:
            if exc.status == 416:  # empty resource
                return RemoteInfo(url=url, size=0, accept_ranges=False)
            raise
        with closing(response):
            headers = response.headers
            size: Optional[int] = None
            ranges = False
            if response.status == 206:
                match = re.match(r"\s*bytes\s+\d+-\d+/(\d+|\*)", headers.get("Content-Range", ""))
                if match and match.group(1) != "*":
                    size, ranges = int(match.group(1)), True
            else:
                length = (headers.get("Content-Length") or "").strip()
                size = int(length) if length.isdigit() else None
            return RemoteInfo(
                url=response.geturl(),
                size=size,
                accept_ranges=ranges,
                etag=headers.get("ETag"),
                last_modified=headers.get("Last-Modified"),
                filename=filename_from_content_disposition(headers.get("Content-Disposition")),
            )

    # ---------------------------------------------------------------- retries

    def _sleep_backoff(self, attempt: int, exc: BaseException, what: str) -> None:
        o = self.options
        delay = min(o.max_backoff, o.backoff * (2 ** (attempt - 1))) * random.uniform(0.75, 1.25)
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            delay = max(delay, retry_after)
        log.warning("%s failed (%s); retry %d/%d in %.1fs", what, exc, attempt, o.retries, delay)
        self._cancel.wait(delay)

    @staticmethod
    def _final_error(exc: BaseException, what: str) -> BaseException:
        if isinstance(exc, PywgetError):
            return exc
        if isinstance(exc, _NETWORK_EXC):
            return NetworkError(f"{what}: {exc}", retryable=False)
        return exc

    def _with_retries(self, func: Callable[[], Any], what: str) -> Any:
        attempt = 0
        while True:
            try:
                return func()
            except Exception as exc:  # noqa: BLE001
                if self._cancel.is_set():
                    raise DownloadCancelled("download cancelled") from None
                if not _is_retryable(exc):
                    raise self._final_error(exc, what) from (None if isinstance(exc, PywgetError) else exc)
                attempt += 1
                if attempt > self.options.retries:
                    raise self._final_error(exc, what) from (None if isinstance(exc, PywgetError) else exc)
                self._sleep_backoff(attempt, exc, what)

    # --------------------------------------------------------------- transfer

    def _read_size(self) -> int:
        o = self.options
        if o.limit_rate:
            return max(4096, min(o.chunk_size, o.limit_rate // 4))
        return o.chunk_size

    def _run_segmented(
        self,
        url: str,
        info: RemoteInfo,
        part: Path,
        state: Path,
        tracker: Tracker,
        limiter: RateLimiter,
        connections: int,
    ) -> Tuple[bool, int]:
        o = self.options
        size = int(info.size or 0)
        segments = self._load_state(state, part, url, info) if o.resume else None
        resumed = segments is not None
        if segments is None:
            _unlink(part, state)
            self._check_space(part.parent, size)
            segments = self._plan(size, connections)
            with open(part, "wb") as handle:
                handle.truncate(size)
        else:
            connections = len(segments)
            log.info("resuming %s (%s already on disk)", part.name, format_size(sum(s[2] for s in segments)))
        tracker.set(sum(s[2] for s in segments))
        todo = [s for s in segments if s[2] < s[1] - s[0] + 1]
        workers = [
            _Worker(
                functools.partial(self._fetch_segment, url, part, seg, size, tracker, limiter),
                f"pywget-{i}",
            )
            for i, seg in enumerate(todo)
        ]
        self._monitor(workers, tracker, lambda: self._save_state(state, url, info, segments))
        if any(s[2] != s[1] - s[0] + 1 for s in segments):
            raise NetworkError("download ended with missing segments")
        return resumed, connections

    @staticmethod
    def _plan(size: int, connections: int) -> List[List[int]]:
        step = size // connections
        return [
            [i * step, (i + 1) * step - 1 if i < connections - 1 else size - 1, 0]
            for i in range(connections)
        ]

    def _fetch_segment(
        self,
        url: str,
        part: Path,
        seg: List[int],
        size: int,
        tracker: Tracker,
        limiter: RateLimiter,
    ) -> None:
        attempt = 0
        read_size = self._read_size()
        while not self._cancel.is_set():
            start = seg[0] + seg[2]
            if start > seg[1]:
                return
            before = seg[2]
            try:
                with closing(self._open(url, {"Range": f"bytes={start}-{seg[1]}"})) as resp:
                    if resp.status != 206 and not (
                        resp.status == 200 and start == 0 and seg[1] == size - 1
                    ):
                        raise NetworkError("server did not honour the Range request", retryable=False)
                    with open(part, "r+b", buffering=0) as handle:
                        handle.seek(start)
                        while not self._cancel.is_set():
                            remaining = seg[1] - (seg[0] + seg[2]) + 1
                            if remaining <= 0:
                                return
                            chunk = resp.read(min(read_size, remaining))
                            if not chunk:
                                raise http.client.IncompleteRead(b"", remaining)
                            limiter.acquire(len(chunk), self._cancel)
                            _write_all(handle, chunk)
                            seg[2] += len(chunk)
                            tracker.add(len(chunk))
            except Exception as exc:  # noqa: BLE001
                if self._cancel.is_set():
                    return
                if not _is_retryable(exc):
                    raise self._final_error(exc, "segment") from (None if isinstance(exc, PywgetError) else exc)
                if seg[2] > before:
                    attempt = 0
                attempt += 1
                if attempt > self.options.retries:
                    raise self._final_error(exc, "segment") from (None if isinstance(exc, PywgetError) else exc)
                self._sleep_backoff(attempt, exc, f"segment {seg[0]}-{seg[1]}")

    def _run_stream(
        self, url: str, info: RemoteInfo, part: Path, tracker: Tracker, limiter: RateLimiter
    ) -> None:
        read_size = self._read_size()

        def attempt_once() -> None:
            tracker.reset()
            total = 0
            with closing(self._open(url)) as resp, open(part, "wb") as handle:
                while True:
                    if self._cancel.is_set():
                        raise DownloadCancelled("download cancelled")
                    chunk = resp.read(read_size)
                    if not chunk:
                        break
                    limiter.acquire(len(chunk), self._cancel)
                    handle.write(chunk)
                    total += len(chunk)
                    tracker.add(len(chunk))
            if info.size is not None and total != info.size:
                raise http.client.IncompleteRead(b"", info.size - total)

        worker = _Worker(lambda: self._with_retries(attempt_once, "download"), "pywget-stream")
        self._monitor([worker], tracker, None)

    def _monitor(
        self, workers: List[_Worker], tracker: Tracker, tick: Optional[Callable[[], None]]
    ) -> None:
        last_tick = 0.0
        try:
            for worker in workers:
                worker.start()
            while True:
                error = next((w.error for w in workers if w.error is not None), None)
                if error is not None:
                    raise error
                alive = [w for w in workers if w.is_alive()]
                if not alive:
                    break
                alive[0].join(0.2)
                self._report(tracker)
                now = time.monotonic()
                if tick and now - last_tick >= 1.0:
                    tick()
                    last_tick = now
        except BaseException:
            self._cancel.set()
            if tick:
                try:
                    tick()
                except Exception:  # noqa: BLE001
                    pass
            for worker in workers:
                if worker.is_alive():
                    worker.join(2.0)
            raise
        if self._cancel.is_set():
            if tick:
                tick()
            raise DownloadCancelled("download cancelled")

    def _report(self, tracker: Tracker, finished: bool = False) -> None:
        callback = self.options.progress
        if callback is None:
            return
        now = time.monotonic()
        if not finished and now - self._last_report < 0.1:
            return
        self._last_report = now
        callback(tracker.snapshot(finished))

    # ------------------------------------------------------------------ state

    @staticmethod
    def _save_state(state: Path, url: str, info: RemoteInfo, segments: List[List[int]]) -> None:
        data = {
            "version": _STATE_VERSION,
            "url": url,
            "size": info.size,
            "etag": info.etag,
            "last_modified": info.last_modified,
            "segments": [list(s) for s in segments],
        }
        tmp = state.with_name(state.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, state)

    @staticmethod
    def _load_state(
        state: Path, part: Path, url: str, info: RemoteInfo
    ) -> Optional[List[List[int]]]:
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            if (
                data["version"] != _STATE_VERSION
                or data["url"] != url
                or data["size"] != info.size
                or (data.get("etag") or None) != (info.etag or None)
                or (data.get("last_modified") or None) != (info.last_modified or None)
            ):
                return None
            segments = [[int(a), int(b), int(c)] for a, b, c in data["segments"]]
            expected_start = 0
            for begin, end, done in sorted(segments):
                if begin != expected_start or end < begin or not 0 <= done <= end - begin + 1:
                    return None
                expected_start = end + 1
            if expected_start != info.size or part.stat().st_size != info.size:
                return None
            return sorted(segments)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _check_space(directory: Path, needed: int) -> None:
        try:
            free = shutil.disk_usage(directory).free
        except OSError:
            return
        if needed > free:
            raise StorageError(
                f"not enough disk space in {directory}: need {format_size(needed)}, "
                f"have {format_size(free)}"
            )

    @staticmethod
    def _fsync(path: Path) -> None:
        try:
            with open(path, "rb") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass

    def _verify(self, path: Path) -> Optional[str]:
        spec = self.options.checksum
        if not spec:
            return None
        algo, expected = parse_checksum(spec)
        actual = hash_file(path, algo)
        if actual != expected:
            raise ChecksumError(f"{algo} mismatch for {path.name}: expected {expected}, got {actual}")
        return f"{algo}:{actual}"

    def _apply_mtime(self, path: Path, last_modified: Optional[str]) -> None:
        if not (self.options.preserve_mtime and last_modified):
            return
        try:
            stamp = parsedate_to_datetime(last_modified).timestamp()
            os.utime(path, (stamp, stamp))
        except (TypeError, ValueError, OverflowError, OSError):
            pass
