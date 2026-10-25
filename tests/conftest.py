#!/usr/bin/env python3

# File: tests/conftest.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: 
# License: MIT

import base64
import http.server
import os
import re
import threading
import time

import pytest

DATA = os.urandom(6 * 1024 * 1024 + 123)
NORANGE = os.urandom(1024 * 1024 + 7)
SMALL = b"hello pywget\n"
ETAG = '"abc123"'


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    flaky_left = 0
    lock = threading.Lock()
    active = 0

    def log_message(self, *args):
        pass

    def _send(self, body, ranges=True, extra=None, head=False):
        start, end, status = 0, len(body) - 1, 200
        rng = self.headers.get("Range")
        if ranges and rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else len(body) - 1, len(body) - 1)
                if start >= len(body):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        chunk = body[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("ETag", ETAG)
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _route(self, head):
        path = self.path.split("?")[0]
        if path == "/big.bin":
            self._send(DATA, head=head)
        elif path == "/small.txt":
            self._send(SMALL, head=head)
        elif path == "/noranges.bin":
            self._send(NORANGE, ranges=False, head=head)
        elif path == "/cd":
            self._send(SMALL, extra={"Content-Disposition": 'attachment; filename="report final.txt"'}, head=head)
        elif path == "/limited":  # allows ONE concurrent request, answers 429 to the rest
            with Handler.lock:
                Handler.active += 1
                busy = Handler.active > 1
            try:
                if busy:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    time.sleep(0.3)
                    self._send(DATA, head=head)
            finally:
                with Handler.lock:
                    Handler.active -= 1
        elif path == "/flaky":
            if Handler.flaky_left > 0:
                Handler.flaky_left -= 1
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(SMALL, head=head)
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/small.txt")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/auth":
            expected = "Basic " + base64.b64encode(b"user:secret").decode()
            if self.headers.get("Authorization") == expected:
                self._send(SMALL, head=head)
            else:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        self._route(False)

    def do_HEAD(self):
        self._route(True)


@pytest.fixture(scope="session")
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def flaky():
    Handler.flaky_left = 2
    yield
    Handler.flaky_left = 0
