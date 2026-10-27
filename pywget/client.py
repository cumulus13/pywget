"""HTTP client for a pywget server / agent."""

from __future__ import annotations

import http.client
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from contextlib import closing
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union
from urllib.parse import quote, urlencode, urlsplit

from .config import DEFAULT_HOST, DEFAULT_PORT, LOOPBACK_HOSTS, load_token
from .errors import ServerError

TERMINAL = ("completed", "failed", "cancelled")
ACTIVE = ("queued", "running")


def _map_http_error(exc: urllib.error.HTTPError) -> ServerError:
    detail = ""
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("error", "")
    except (ValueError, OSError, AttributeError):
        pass
    exc.close()
    hint = " (missing/invalid token: use --token, $PYWGET_TOKEN or the token file)" if exc.code == 401 else ""
    return ServerError(
        f"server answered HTTP {exc.code}: {detail or exc.reason}{hint}",
        status=exc.code,
        retryable=exc.code in (502, 503, 504),
    )


class ServerClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 15.0,
        verify: bool = True,
        ca_file: Optional[str] = None,
    ) -> None:
        base = base_url or os.environ.get("PYWGET_SERVER") or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
        if "://" not in base:
            base = "http://" + base
        self.base = base.rstrip("/")
        self.token = token or load_token()
        self.timeout = timeout
        parts = urlsplit(self.base)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ServerError(f"invalid server URL: {base!r}")
        handlers: List[Any] = []
        if parts.hostname in LOOPBACK_HOSTS:
            handlers.append(urllib.request.ProxyHandler({}))
        if parts.scheme == "https":
            ctx = ssl.create_default_context(cafile=ca_file)
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _map_http_error(exc) from None
        except urllib.error.URLError as exc:
            raise ServerError(f"cannot reach {self.base}: {exc.reason}", retryable=True) from None
        except (OSError, http.client.HTTPException) as exc:
            raise ServerError(f"cannot reach {self.base}: {exc}", retryable=True) from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ServerError("server returned invalid JSON") from None

    # ----------------------------------------------------------------- API

    def health(self) -> Dict[str, Any]:
        return self._call("GET", "/health")

    def stats(self) -> Dict[str, Any]:
        return self._call("GET", "/api/v1/stats")

    def submit(self, specs: Union[Dict[str, Any], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        body = {"jobs": specs} if isinstance(specs, list) else specs
        return self._call("POST", "/api/v1/jobs", body)["jobs"]

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "?" + urlencode({"status": status}) if status else ""
        return self._call("GET", "/api/v1/jobs" + query)["jobs"]

    def get(self, job_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/api/v1/jobs/{quote(job_id)}")

    def action(self, job_id: str, name: str) -> Dict[str, Any]:
        return self._call("POST", f"/api/v1/jobs/{quote(job_id)}/{name}")

    def remove(self, job_id: str) -> None:
        self._call("DELETE", f"/api/v1/jobs/{quote(job_id)}")

    def wait(
        self,
        job_id: str,
        interval: float = 1.0,
        timeout: Optional[float] = None,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            job = self.get(job_id)
            if callback:
                callback(job)
            if job["status"] in TERMINAL:
                return job
            if deadline and time.monotonic() > deadline:
                raise ServerError(f"timed out waiting for job {job_id}")
            time.sleep(interval)

    def wait_all(
        self,
        job_ids: Sequence[str],
        interval: float = 1.0,
        timeout: Optional[float] = None,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Poll until every job in *job_ids* is finished; return their final states."""
        final: Dict[str, Dict[str, Any]] = {}
        for job in self.watch(job_ids, interval=interval, timeout=timeout):
            if callback:
                callback(job)
            final[job["id"]] = job
        return [final[job_id] for job_id in job_ids]

    def watch(
        self,
        job_ids: Optional[Sequence[str]] = None,
        interval: float = 1.0,
        timeout: Optional[float] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Poll and yield a job each time its status, progress or error changes.

        With *job_ids* the iterator ends when all of them are finished (completed / failed /
        cancelled). Without ids it watches every job and ends when nothing is queued or running.
        """
        interval = max(0.2, float(interval))
        deadline = time.monotonic() + timeout if timeout else None
        seen: Dict[str, tuple] = {}
        while True:
            jobs = [self.get(job_id) for job_id in job_ids] if job_ids else self.list()
            for job in jobs:
                key = (job["status"], job.get("percent"), job.get("error"), job.get("remote_status"))
                if seen.get(job["id"]) != key:
                    seen[job["id"]] = key
                    yield job
            if job_ids:
                finished = all(job["status"] in TERMINAL for job in jobs)
            else:
                finished = not any(job["status"] in ACTIVE for job in jobs)
            if finished:
                return
            if deadline and time.monotonic() > deadline:
                raise ServerError("timed out while watching jobs")
            time.sleep(interval)

    def events(self, timeout: float = 30.0) -> Iterator[Dict[str, Any]]:
        """Server-sent events: yields ``{"event": "job", "job": {...}}`` as the server pushes them."""
        headers = {"Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base + "/api/v1/events", headers=headers)
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise _map_http_error(exc) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise ServerError(f"cannot reach {self.base}: {exc}", retryable=True) from None
        with closing(response):
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    try:
                        yield json.loads(line[5:].strip())
                    except ValueError:
                        continue
