#!/usr/bin/env python3

# File: pywget/cli.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: Command line interface.
# License: MIT

"""Command line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sys
from typing import Any, Dict, List, Optional

from .__version__ import __version__
from .core import Downloader, Options
from .errors import PywgetError
from .progress import ProgressBar
from .utils import format_size

EPILOG = """\
examples:
  pywget https://example.com/file.iso
  pywget https://example.com/file.iso -p ~/Downloads -n 8
  pywget -i urls.txt --limit-rate 2M --checksum sha256:<hex>
  pywget URL -H "Authorization: Bearer $TOKEN" -O out.bin
  cat urls.txt | pywget -i - --json

exit codes: 0 ok | 2 bad usage/URL | 3 network | 4 HTTP status | 5 checksum
            6 storage/I-O | 130 interrupted
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pywget",
        description="Robust, resumable, multi-connection wget alternative.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("urls", nargs="*", metavar="URL", help="http(s) URLs to download")
    p.add_argument("-i", "--input-file", metavar="FILE", help="read URLs from FILE ('-' = stdin)")
    p.add_argument("-p", "-P", "--path", "--directory-prefix", dest="path", default=".",
                   metavar="DIR", help="download directory (default: .)")
    p.add_argument("-O", "--output", metavar="NAME", help="output file name (single URL only)")
    p.add_argument("-n", "--connections", type=int, default=4, metavar="N",
                   help="parallel connections per file, 1-32 (default: 4)")
    p.add_argument("-r", "--retries", type=int, default=5, metavar="N",
                   help="retries per request/segment (default: 5)")
    p.add_argument("-T", "--timeout", type=float, default=30.0, metavar="SEC",
                   help="network timeout in seconds (default: 30)")
    p.add_argument("--limit-rate", metavar="RATE", help="max total speed, e.g. 500k, 2M")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'K: V'",
                   help="extra request header (repeatable)")
    p.add_argument("-U", "--user-agent", metavar="UA", help="custom User-Agent")
    p.add_argument("--user", metavar="NAME", help="HTTP basic-auth user name")
    p.add_argument("--password", metavar="PASS",
                   help="HTTP basic-auth password (or env PYWGET_PASSWORD, or prompt)")
    p.add_argument("--proxy", metavar="URL", help="proxy URL (default: environment)")
    p.add_argument("--no-check-certificate", action="store_true", help="skip TLS verification")
    p.add_argument("--ca-cert", metavar="FILE", help="custom CA bundle")
    p.add_argument("--checksum", metavar="ALGO:HEX", help="verify downloaded file, e.g. sha256:ab12..")
    p.add_argument("--overwrite", action="store_true", help="replace existing file, restart download")
    p.add_argument("--no-resume", action="store_true", help="never resume partial downloads")
    p.add_argument("-c", "--continue", dest="cont", action="store_true",
                   help="accepted for wget compatibility (resume is on by default)")
    p.add_argument("--no-mtime", action="store_true", help="do not copy Last-Modified to the file")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress or status output")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--json", action="store_true", help="print machine-readable results to stdout")
    p.add_argument("-V", "--version", action="version", version=f"pywget {__version__}")
    return p


def _read_urls(source: str) -> List[str]:
    handle = sys.stdin if source == "-" else open(source, encoding="utf-8")
    try:
        lines = [line.strip() for line in handle]
    finally:
        if handle is not sys.stdin:
            handle.close()
    return [line for line in lines if line and not line.startswith("#")]


def _parse_headers(items: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"invalid header (expected 'Name: value'): {item!r}")
        headers[name.strip()] = value.strip()
    return headers


def _build_options(args: argparse.Namespace, progress: Optional[ProgressBar]) -> Options:
    password = args.password or os.environ.get("PYWGET_PASSWORD")
    if args.user and password is None and sys.stdin.isatty():
        password = getpass.getpass(f"Password for {args.user}: ")
    extra: Dict[str, Any] = {}
    if args.user_agent:
        extra["user_agent"] = args.user_agent
    return Options(
        output_dir=args.path,
        connections=args.connections,
        retries=args.retries,
        timeout=args.timeout,
        limit_rate=args.limit_rate or 0,
        resume=not args.no_resume,
        overwrite=args.overwrite,
        preserve_mtime=not args.no_mtime,
        headers=_parse_headers(args.header),
        username=args.user,
        password=password,
        proxy=args.proxy,
        verify_ssl=not args.no_check_certificate,
        ca_file=args.ca_cert,
        checksum=args.checksum,
        progress=progress,
        **extra,
    )


def _on_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    urls = list(args.urls)
    if args.input_file:
        try:
            urls += _read_urls(args.input_file)
        except OSError as exc:
            parser.error(f"cannot read input file: {exc}")
    if not urls:
        parser.error("no URLs given")
    if args.output and len(urls) > 1:
        parser.error("--output can only be used with a single URL")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="pywget: %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):  # not in main thread / unsupported platform
        pass

    bar = ProgressBar(enabled=not args.quiet)
    try:
        options = _build_options(args, bar)
    except ValueError as exc:
        parser.error(str(exc))
    downloader = Downloader(options)

    results: List[Dict[str, Any]] = []
    exit_code = 0
    for url in urls:
        try:
            result = downloader.fetch(url, filename=args.output)
        except KeyboardInterrupt:
            bar.close()
            print("pywget: interrupted; partial data kept, re-run to resume", file=sys.stderr)
            return 130
        except (PywgetError, OSError) as exc:
            bar.close()
            print(f"pywget: error: {exc}", file=sys.stderr)
            results.append({"ok": False, "url": url, "error": str(exc)})
            exit_code = exit_code or getattr(exc, "exit_code", 6)
            continue
        results.append({"ok": True, **result.to_dict()})
        if not args.quiet:
            extra = ", skipped (already complete)" if result.skipped else ""
            extra += ", resumed" if result.resumed else ""
            print(
                f"OK  {result.path}  ({format_size(result.size)}, {result.elapsed:.1f}s, "
                f"{format_size(result.speed)}/s, {result.connections} conn{extra})",
                file=sys.stderr,
            )
    if args.json:
        print(json.dumps(results, indent=2))
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
