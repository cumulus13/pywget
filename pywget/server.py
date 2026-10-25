"""HTTP API server (server mode and agent/relay mode)."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import queue
import re
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlsplit

from .__version__ import __version__
from .config import LOOPBACK_HOSTS, ServerConfig
from .jobs import JobManager, JobNotFound, JobStateError, parse_specs
from .webui import INDEX_HTML

log = logging.getLogger("pywget.server")

MAX_BODY = 1024 * 1024
_JOB_RE = re.compile(r"^/api/v1/jobs/([A-Za-z0-9_-]{1,64})(?:/(pause|resume|cancel|retry))?$")


class Forbidden(Exception):
    pass


class PywgetHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg: ServerConfig, manager: JobManager) -> None:
        self.cfg = cfg
        self.manager = manager
        self.shutting_down = threading.Event()
        self.allowed_hosts = self._allowed_hosts(cfg)
        if ":" in cfg.host:
            self.address_family = socket.AF_INET6
        super().__init__((cfg.host, cfg.port), ApiHandler)
        if cfg.tls_cert and cfg.tls_key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
            self.socket = ctx.wrap_socket(self.socket, server_side=True)

    @staticmethod
    def _allowed_hosts(cfg: ServerConfig) -> Optional[Set[str]]:
        wildcard = cfg.host in ("0.0.0.0", "::", "")
        if wildcard and not cfg.allowed_hosts:
            return None  # reachable by any name; token auth is the protection
        names = {h.lower() for h in cfg.allowed_hosts} | {h.lower() for h in LOOPBACK_HOSTS}
        if not wildcard:
            names.add(cfg.host.lower())
        return names

    @property
    def url(self) -> str:
        scheme = "https" if self.cfg.tls_cert else "http"
        host = self.cfg.host if self.cfg.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"{scheme}://{host}:{self.server_address[1]}"


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"pywget/{__version__}"
    server: PywgetHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------ plumbing

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        allowed = self.server.allowed_hosts
        if allowed is None:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[1:].split("]")[0]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return host in allowed

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Pywget-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            supplied = auth[7:].strip()
        return bool(supplied) and hmac.compare_digest(supplied.encode(), self.server.cfg.token.encode())

    def _read_json(self) -> Any:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise ValueError("chunked request bodies are not supported")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise OverflowError("request body too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("body is not valid JSON") from None

    def _check_target(self, urls: List[str]) -> None:
        if not self.server.cfg.deny_private:
            return
        for url in urls:
            host = urlsplit(url).hostname or ""
            try:
                infos = socket.getaddrinfo(host, None)
            except socket.gaierror:
                raise ValueError(f"cannot resolve host: {host}") from None
            for info in infos:
                address = ipaddress.ip_address(str(info[4][0]).split("%")[0])
                if not address.is_global:
                    raise Forbidden(f"target {host} resolves to a non-public address")

    # ------------------------------------------------------------- routing

    def do_GET(self) -> None:
        self._handle()

    do_POST = do_DELETE = do_HEAD = do_GET

    def do_OPTIONS(self) -> None:  # no CORS: browsers must not script this API cross-origin
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self) -> None:
        try:
            if not self._host_ok():
                return self._error(421, "unexpected Host header")
            parts = urlsplit(self.path)
            path, method = parts.path, self.command
            mgr = self.server.manager
            if path == "/" and method in ("GET", "HEAD"):
                if not self.server.cfg.ui:
                    return self._error(404, "web UI disabled")
                return self._send(
                    200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8",
                    {
                        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; "
                        "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'",
                        "X-Frame-Options": "DENY",
                        "Referrer-Policy": "no-referrer",
                    },
                )
            if path == "/health" and method in ("GET", "HEAD"):
                return self._json(200, {
                    "status": "ok", "version": __version__,
                    "mode": "agent" if self.server.cfg.is_agent else "server",
                })
            if not path.startswith("/api/v1/"):
                return self._error(404, "not found")
            if not self._authorized():
                return self._send(
                    401, json.dumps({"error": "unauthorized"}).encode(), "application/json",
                    {"WWW-Authenticate": 'Bearer realm="pywget"'},
                )
            if path == "/api/v1/stats" and method == "GET":
                return self._json(200, mgr.stats())
            if path == "/api/v1/events" and method == "GET":
                return self._events()
            if path == "/api/v1/jobs":
                if method == "GET":
                    status = (parse_qs(parts.query).get("status") or [""])[0] or None
                    return self._json(200, {"jobs": [j.to_public() for j in mgr.list(status)]})
                if method == "POST":
                    root = None if self.server.cfg.is_agent else self.server.cfg.root
                    specs = parse_specs(self._read_json(), root)
                    self._check_target([s["url"] for s in specs])
                    jobs = [mgr.add(spec) for spec in specs]
                    return self._json(201, {"jobs": [j.to_public() for j in jobs]})
                return self._error(405, "method not allowed")
            match = _JOB_RE.match(path)
            if match:
                job_id, action = match.groups()
                if action is None and method == "GET":
                    return self._json(200, mgr.get(job_id).to_public())
                if action is None and method == "DELETE":
                    mgr.remove(job_id)
                    return self._json(200, {"removed": job_id})
                if action and method == "POST":
                    return self._json(200, getattr(mgr, action)(job_id).to_public())
                return self._error(405, "method not allowed")
            return self._error(404, "not found")
        except JobNotFound as exc:
            self._error(404, str(exc))
        except JobStateError as exc:
            self._error(409, str(exc))
        except Forbidden as exc:
            self._error(403, str(exc))
        except OverflowError as exc:
            self._error(413, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            log.exception("unhandled error in %s %s", self.command, self.path)
            try:
                self._error(500, "internal server error")
            except OSError:
                self.close_connection = True

    def _events(self) -> None:
        sub = self.server.manager.events.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.server.shutting_down.is_set():
                try:
                    event = sub.get(timeout=1.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"event: {event['event']}\ndata: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.manager.events.unsubscribe(sub)


class ServerHandle:
    """A running server (blocking :func:`run_server` or background thread for tests/embedding)."""

    def __init__(self, cfg: ServerConfig) -> None:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        if not cfg.is_agent:
            cfg.root.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.manager = JobManager(cfg)
        self.httpd = PywgetHTTPServer(cfg, self.manager)
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return self.httpd.url

    def start(self) -> ServerHandle:
        self.manager.start()
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.manager.start()
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        finally:
            self.close()

    def close(self) -> None:
        self.httpd.shutting_down.set()
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(5.0)
        self.manager.stop()
        self.httpd.server_close()

    def __enter__(self) -> ServerHandle:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()


def run_server(cfg: ServerConfig) -> int:
    handle = ServerHandle(cfg)
    kind = "agent (relay)" if cfg.is_agent else "server"
    import sys

    print(f"pywget {kind} listening on {handle.url}", file=sys.stderr)
    if cfg.token_source:
        print(f"api token: {cfg.token_source}  (show it with: pywget token show)", file=sys.stderr)
    if cfg.is_agent:
        print(f"forwarding links to {cfg.upstream}", file=sys.stderr)
    else:
        print(f"download root: {cfg.root}", file=sys.stderr)
    if cfg.host not in LOOPBACK_HOSTS and not cfg.tls_cert:
        print("warning: non-loopback bind without TLS - put it behind HTTPS (reverse proxy) or use --tls-cert", file=sys.stderr)
    try:
        handle.serve_forever()
    except KeyboardInterrupt:
        print("shutting down (running jobs are saved and will resume)", file=sys.stderr)
    return 0
