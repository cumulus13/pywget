"""Job model, persistence, scheduling and executors for server / agent mode."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import TERMINAL, ServerClient
from .config import ServerConfig
from .core import Downloader, Options
from .errors import DownloadCancelled, InvalidURLError, PywgetError, ServerError
from .progress import ProgressInfo
from .utils import RateLimiter, build_headers, normalize_url, parse_checksum, parse_size

log = logging.getLogger("pywget.jobs")

SPEC_FIELDS = {
    "url", "urls", "jobs", "filename", "path", "referrer", "cookie", "headers", "user_agent",
    "username", "password", "connections", "checksum", "limit_rate", "overwrite", "add_only",
}
_SECRET_FIELDS = ("password", "cookie", "headers")


class JobNotFound(LookupError):
    pass


class JobStateError(Exception):
    pass


@dataclass
class Job:
    id: str
    url: str
    filename: Optional[str] = None
    path: str = ""
    referrer: Optional[str] = None
    cookie: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    user_agent: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    connections: Optional[int] = None
    checksum: Optional[str] = None
    limit_rate: int = 0
    overwrite: bool = False
    status: str = "queued"
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    size: Optional[int] = None
    downloaded: int = 0
    speed: float = 0.0
    eta: Optional[float] = None
    error: Optional[str] = None
    result_path: Optional[str] = None
    checksum_result: Optional[str] = None
    partial: List[str] = field(default_factory=list)
    remote_id: Optional[str] = None
    remote_status: Optional[str] = None
    stop_reason: Optional[str] = field(default=None, repr=False)  # transient

    def to_dict(self) -> Dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "stop_reason"}
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Job:
        known = {f.name for f in fields(cls)} - {"stop_reason"}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_public(self) -> Dict[str, Any]:
        data = self.to_dict()
        for name in _SECRET_FIELDS:
            data.pop(name, None)
        data["has_auth"] = bool(self.username)
        data["has_cookie"] = bool(self.cookie)
        data["header_names"] = sorted(self.headers)
        pct = None
        if self.size:
            pct = round(min(100.0, self.downloaded * 100.0 / self.size), 1)
        elif self.status == "completed":
            pct = 100.0
        data["percent"] = pct
        return data

    def to_spec(self) -> Dict[str, Any]:
        """Creation spec including secrets (used to forward to an upstream server)."""
        spec = {
            "url": self.url, "filename": self.filename, "path": self.path, "referrer": self.referrer,
            "cookie": self.cookie, "headers": self.headers or None, "user_agent": self.user_agent,
            "username": self.username, "password": self.password, "connections": self.connections,
            "checksum": self.checksum, "limit_rate": self.limit_rate or None,
            "overwrite": self.overwrite or None,
        }
        return {k: v for k, v in spec.items() if v not in (None, "")}


def resolve_dest(root: Path, rel: str) -> Path:
    """Resolve *rel* below *root*; refuse anything that escapes it."""
    root_r = Path(root).expanduser().resolve()
    target = (root_r / rel).resolve() if rel else root_r
    if target != root_r and root_r not in target.parents:
        raise ValueError(f"path escapes the server root: {rel!r}")
    return target


def _str(value: Any, name: str, limit: int = 4096) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"'{name}' must be a string (max {limit} chars)")
    return value or None


def _validate_one(spec: Dict[str, Any], root: Optional[Path]) -> Dict[str, Any]:
    unknown = set(spec) - SPEC_FIELDS
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if not isinstance(spec.get("url"), str):
        raise ValueError("'url' is required")
    try:
        url, url_user, url_pass = normalize_url(spec["url"])
    except InvalidURLError as exc:
        raise ValueError(str(exc)) from None
    out: Dict[str, Any] = {"url": url}
    out["filename"] = _str(spec.get("filename"), "filename", 255)
    out["path"] = _str(spec.get("path"), "path") or ""
    if root is not None:
        resolve_dest(root, out["path"])
    out["referrer"] = _str(spec.get("referrer"), "referrer")
    out["user_agent"] = _str(spec.get("user_agent"), "user_agent", 512)
    out["username"] = _str(spec.get("username"), "username", 512) or url_user
    out["password"] = _str(spec.get("password"), "password", 512) or url_pass
    cookie = spec.get("cookie")
    if isinstance(cookie, dict):
        cookie = "; ".join(f"{k}={v}" for k, v in cookie.items())
    out["cookie"] = _str(cookie, "cookie")
    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("'headers' must be an object")
    out["headers"] = {str(k): str(v) for k, v in headers.items()}
    build_headers(out["referrer"], out["cookie"], out["headers"])  # injection check
    if spec.get("connections") is not None:
        conns = spec["connections"]
        if not isinstance(conns, int) or isinstance(conns, bool) or not 1 <= conns <= 32:
            raise ValueError("'connections' must be an integer between 1 and 32")
        out["connections"] = conns
    if spec.get("checksum"):
        parse_checksum(_str(spec["checksum"], "checksum") or "")
        out["checksum"] = spec["checksum"]
    out["limit_rate"] = parse_size(spec["limit_rate"]) if spec.get("limit_rate") else 0
    for flag in ("overwrite", "add_only"):
        if spec.get(flag) is not None and not isinstance(spec[flag], bool):
            raise ValueError(f"'{flag}' must be true or false")
    out["overwrite"] = bool(spec.get("overwrite"))
    out["add_only"] = bool(spec.get("add_only"))
    return out


def parse_specs(data: Any, root: Optional[Path]) -> List[Dict[str, Any]]:
    """Validate a POST /jobs body: single spec, {"urls": [...]} or {"jobs": [...]}."""
    if not isinstance(data, dict):
        raise ValueError("JSON object expected")
    if "jobs" in data:
        items = data["jobs"]
        if set(data) != {"jobs"} or not isinstance(items, list) or not items:
            raise ValueError("'jobs' must be a non-empty list and the only field")
        specs = items
    elif "urls" in data:
        urls = data["urls"]
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
            raise ValueError("'urls' must be a non-empty list of strings")
        if len(urls) > 1 and data.get("filename"):
            raise ValueError("'filename' cannot be combined with several urls")
        common = {k: v for k, v in data.items() if k != "urls"}
        specs = [dict(common, url=u) for u in urls]
    else:
        specs = [data]
    if len(specs) > 500:
        raise ValueError("too many jobs in one request (max 500)")
    if not all(isinstance(s, dict) for s in specs):
        raise ValueError("every job must be an object")
    return [_validate_one(s, root) for s in specs]


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> List[Job]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [Job.from_dict(item) for item in data.get("jobs", [])]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError) as exc:
            log.error("cannot read %s (%s); starting with an empty queue", self.path, exc)
            return []

    def save(self, jobs: List[Job]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps({"version": 1, "jobs": [j.to_dict() for j in jobs]}, indent=1)
        fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)  # holds credentials
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, self.path)


class EventBus:
    def __init__(self) -> None:
        self._subs: List[queue.Queue[Dict[str, Any]]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[Dict[str, Any]]:
        sub: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=512)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue[Dict[str, Any]]) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.put_nowait(event)
            except queue.Full:
                pass


class LocalExecutor:
    """Runs jobs with the pywget download engine."""

    def __init__(self, manager: JobManager) -> None:
        self.mgr = manager
        self._active: Dict[str, Downloader] = {}
        self._lock = threading.Lock()

    def run(self, job: Job) -> None:
        cfg = self.mgr.cfg
        dest = resolve_dest(cfg.root, job.path)
        kwargs: Dict[str, Any] = dict(
            output_dir=str(dest),
            filename=job.filename,
            connections=job.connections or cfg.connections,
            retries=cfg.retries,
            headers=build_headers(job.referrer, job.cookie, job.headers),
            username=job.username,
            password=job.password,
            checksum=job.checksum,
            overwrite=job.overwrite,
            limit_rate=job.limit_rate or 0,
            rate_limiter=self.mgr.limiter,
        )
        if job.user_agent:
            kwargs["user_agent"] = job.user_agent
        holder: Dict[str, Downloader] = {}

        def on_progress(info: ProgressInfo) -> None:
            self.mgr.progress(job, info)
            if job.stop_reason and "dl" in holder:
                holder["dl"].cancel()

        downloader = Downloader(Options(progress=on_progress, **kwargs))
        holder["dl"] = downloader
        with self._lock:
            self._active[job.id] = downloader
        try:
            if job.stop_reason:
                raise DownloadCancelled("stopped before start")
            result = downloader.fetch(job.url)
            self.mgr.update(
                job, size=result.size, downloaded=result.size, result_path=str(result.path),
                checksum_result=result.checksum, partial=[], eta=0.0,
            )
        finally:
            with self._lock:
                self._active.pop(job.id, None)
            if downloader.partial and job.status == "running":
                job.partial = [str(p) for p in downloader.partial]

    def interrupt(self, job: Job) -> None:
        with self._lock:
            downloader = self._active.get(job.id)
        if downloader:
            downloader.cancel()


class RelayExecutor:
    """Agent mode: forwards jobs to an upstream server and mirrors their progress."""

    def __init__(self, manager: JobManager, client: ServerClient, poll: float = 1.0) -> None:
        self.mgr = manager
        self.client = client
        self.poll = poll

    def _stopped(self, job: Job) -> bool:
        return bool(job.stop_reason) or self.mgr.stopping

    def run(self, job: Job) -> None:
        delay = 1.0
        while not self._stopped(job):
            try:
                if not job.remote_id:
                    remote = self.client.submit(job.to_spec())[0]
                    self.mgr.update(job, remote_id=remote["id"], error=None)
                    self.mgr.save()
                remote = self.client.get(job.remote_id)  # type: ignore[arg-type]
                delay = 1.0
                self.mgr.mirror(job, remote)
                status = remote["status"]
                if status == "completed":
                    return
                if status == "failed":
                    raise PywgetError(remote.get("error") or "remote job failed")
                if status == "cancelled":
                    raise DownloadCancelled("cancelled upstream")
                if status == "paused" and not self._stopped(job):
                    self.client.action(job.remote_id, "resume")  # type: ignore[arg-type]
            except ServerError as exc:
                if exc.status == 404 and job.remote_id:
                    self.mgr.update(job, remote_id=None)  # upstream lost it: resubmit
                    continue
                if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
                    raise
                self.mgr.update(job, error=f"upstream unavailable, retrying: {exc}")
                self._sleep(job, delay)
                delay = min(delay * 2, 30.0)
                continue
            self._sleep(job, self.poll)
        self._forward_stop(job)
        raise DownloadCancelled("stopped")

    def _sleep(self, job: Job, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stopped(job):
            time.sleep(0.1)

    def _forward_stop(self, job: Job) -> None:
        action = {"pause": "pause", "cancel": "cancel"}.get(job.stop_reason or "")
        if action and job.remote_id:
            try:
                self.client.action(job.remote_id, action)
            except ServerError as exc:
                log.warning("could not forward %s to upstream: %s", action, exc)

    def interrupt(self, job: Job) -> None:  # the run loop notices stop_reason itself
        pass


class JobManager:
    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.jobs: Dict[str, Job] = {}
        self.events = EventBus()
        self.store = JobStore(cfg.state_dir / "jobs.json")
        self.limiter: Optional[RateLimiter] = (
            RateLimiter(parse_size(cfg.limit_rate)) if cfg.limit_rate else None
        )
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._last_publish: Dict[str, float] = {}
        self.executor: Any
        if cfg.upstream:
            client = ServerClient(
                cfg.upstream, cfg.upstream_token, verify=cfg.upstream_verify, ca_file=cfg.upstream_ca
            )
            self.executor = RelayExecutor(self, client)
        else:
            self.executor = LocalExecutor(self)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        for job in self.store.load():
            if job.status == "running":
                job.status = "queued"
            self.jobs[job.id] = job
            if job.status == "queued":
                self._queue.put(job.id)
        for index in range(self.cfg.max_jobs):
            thread = threading.Thread(target=self._worker, name=f"pywget-job-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            running = [j for j in self.jobs.values() if j.status == "running"]
            for job in running:
                job.stop_reason = "shutdown"
        for job in running:
            self.executor.interrupt(job)
        for thread in self._threads:
            thread.join(15.0)
        self.save()

    def save(self) -> None:
        with self._lock:
            try:
                self.store.save(list(self.jobs.values()))
            except OSError as exc:
                log.error("cannot save job state: %s", exc)

    # ----------------------------------------------------------- operations

    def add(self, spec: Dict[str, Any]) -> Job:
        add_only = spec.pop("add_only", False)
        job = Job(id=uuid.uuid4().hex[:12], **spec)
        job.status = "paused" if add_only else "queued"
        with self._lock:
            self.jobs[job.id] = job
        if not add_only:
            self._queue.put(job.id)
        self.save()
        self._publish(job)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            try:
                return self.jobs[job_id]
            except KeyError:
                raise JobNotFound(f"no such job: {job_id}") from None

    def list(self, status: Optional[str] = None) -> List[Job]:
        with self._lock:
            items = sorted(self.jobs.values(), key=lambda j: j.created)
        return [j for j in items if not status or j.status == status]

    def pause(self, job_id: str) -> Job:
        job = self.get(job_id)
        interrupt = False
        with self._lock:
            if job.status == "queued":
                job.status = "paused"
            elif job.status == "running":
                job.stop_reason = "pause"
                interrupt = True
            elif job.status != "paused":
                raise JobStateError(f"cannot pause a {job.status} job")
        if interrupt:
            self.executor.interrupt(job)
        self.save()
        self._publish(job)
        return job

    def resume(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self._lock:
            if job.status == "running" and job.stop_reason:
                raise JobStateError("job is still stopping; try again in a moment")
            if job.status != "paused":
                raise JobStateError(f"cannot resume a {job.status} job")
            job.status = "queued"
            self._queue.put(job.id)
        self.save()
        self._publish(job)
        return job

    def retry(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self._lock:
            if job.status not in ("failed", "cancelled"):
                raise JobStateError(f"cannot retry a {job.status} job")
            job.status, job.error, job.remote_id, job.remote_status = "queued", None, None, None
            job.downloaded, job.speed, job.eta, job.finished = 0, 0.0, None, None
            self._queue.put(job.id)
        self.save()
        self._publish(job)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        interrupt = False
        with self._lock:
            if job.status in ("queued", "paused"):
                job.status, job.finished = "cancelled", time.time()
                self._cleanup(job)
            elif job.status == "running":
                job.stop_reason = "cancel"
                interrupt = True
            else:
                raise JobStateError(f"cannot cancel a {job.status} job")
        if interrupt:
            self.executor.interrupt(job)
        self.save()
        self._publish(job)
        return job

    def remove(self, job_id: str) -> None:
        job = self.get(job_id)
        with self._lock:
            if job.status not in TERMINAL:
                raise JobStateError(f"cannot remove a {job.status} job (cancel it first)")
            del self.jobs[job_id]
        self.save()
        self.events.publish({"event": "removed", "id": job_id})

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            jobs = list(self.jobs.values())
        counts: Dict[str, int] = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        return {
            "mode": "agent" if self.cfg.is_agent else "server",
            "jobs": counts,
            "total": len(jobs),
            "speed": sum(j.speed for j in jobs if j.status == "running"),
            "downloaded_bytes": sum(j.downloaded for j in jobs),
        }

    # ------------------------------------------------- executor-facing API

    def update(self, job: Job, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)

    def progress(self, job: Job, info: ProgressInfo) -> None:
        self.update(job, size=info.total, downloaded=info.downloaded, speed=info.speed, eta=info.eta)
        self._publish(job, throttle=True)

    def mirror(self, job: Job, remote: Dict[str, Any]) -> None:
        self.update(
            job,
            remote_status=remote.get("status"),
            size=remote.get("size"),
            downloaded=remote.get("downloaded", 0),
            speed=remote.get("speed", 0.0),
            eta=remote.get("eta"),
            result_path=remote.get("result_path"),
            checksum_result=remote.get("checksum_result"),
            error=None,
        )
        self._publish(job, throttle=True)

    # ----------------------------------------------------------- internals

    def _cleanup(self, job: Job) -> None:
        for name in job.partial:
            try:
                Path(name).unlink()
            except OSError:
                pass
        job.partial = []

    def _publish(self, job: Job, throttle: bool = False) -> None:
        now = time.monotonic()
        if throttle and now - self._last_publish.get(job.id, 0.0) < 0.5:
            return
        self._last_publish[job.id] = now
        self.events.publish({"event": "job", "job": job.to_public()})

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                job = self.jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue
                job.status, job.started, job.error, job.stop_reason = "running", time.time(), None, None
            self._publish(job)
            self._run(job)

    def _run(self, job: Job) -> None:
        outcome, error = "completed", None
        try:
            self.executor.run(job)
        except DownloadCancelled:
            outcome = {"pause": "paused", "shutdown": "queued"}.get(job.stop_reason or "", "cancelled")
        except (PywgetError, OSError, ValueError) as exc:
            outcome, error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - never kill a worker thread
            log.exception("job %s crashed", job.id)
            outcome, error = "failed", f"internal error: {exc}"
        with self._lock:
            job.status, job.error, job.speed = outcome, error, 0.0
            if outcome in TERMINAL:
                job.finished = time.time()
            if outcome == "completed":
                job.downloaded = job.size or job.downloaded
            if outcome == "cancelled":
                self._cleanup(job)
            job.stop_reason = None
        self.save()
        self._publish(job)
