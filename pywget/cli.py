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
import shutil
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .__version__ import __version__
from .client import ServerClient
from .clipboard import extract_urls, read_clipboard
from .config import (
    DEFAULT_AGENT_PORT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ServerConfig,
    config_dir,
    config_file_path,
    generate_token,
    load_config,
    resolve_token,
    save_token,
    token_path,
    validate_token,
    write_default_config,
)
from .core import Downloader, Options
from .errors import ConfigError, PywgetError
from .progress import ProgressBar
from .utils import build_headers, format_size

EPILOG = """\
examples:
  pywget https://example.com/file.iso
  pywget https://example.com/file.iso -p ~/Downloads -n 8 -r https://example.com/
  pywget -i urls.txt --limit-rate 2M --checksum sha256:<hex>
  pywget URL -H "Authorization: Bearer $TOKEN" -O out.bin
  pywget URL --server http://nas:8765          # send the link to a pywget server/agent

other commands (pywget COMMAND -h):
  serve   run the download server (HTTP API + queue + web UI)
  agent   run a relay that forwards links to an upstream server
  send    send links to a server/agent ("c" = clipboard, or a text file of links)
  jobs    list / show / pause / resume / cancel / retry / remove server jobs
  token   show / new / set / path   - the API token (auto-generated on first serve)
  config  path / init / show        - the optional config file (config.ini)

exit codes: 0 ok | 2 bad usage/URL | 3 network | 4 HTTP status | 5 checksum
            6 storage/I-O | 7 server/agent error | 130 interrupted
"""

COMMANDS = ("serve", "agent", "send", "jobs", "token", "config")


# ----------------------------------------------------------------- shared bits


def _add_request_args(p: argparse.ArgumentParser, local: bool) -> None:
    p.add_argument("-r", "--referrer", metavar="URL", help="Referer header (many sites require it)")
    p.add_argument("-C", "--cookie", metavar="COOKIE", help="cookie string 'a=b; c=d' or JSON object")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'K: V'",
                   help="extra request header (repeatable)")
    p.add_argument("-U", "-ua", "--user-agent", dest="user_agent", metavar="UA", help="custom User-Agent")
    p.add_argument("--user", metavar="NAME", help="HTTP basic-auth user name for the file")
    p.add_argument("--password", metavar="PASS",
                   help="basic-auth password (or env PYWGET_PASSWORD, or prompt)")
    p.add_argument("-n", "--connections", type=int, default=None, metavar="N",
                   help="parallel connections per file, 1-32" + (" (default: 4)" if local else ""))
    p.add_argument("--limit-rate", metavar="RATE", help="max speed, e.g. 500k, 2M")
    p.add_argument("--checksum", metavar="ALGO:HEX", help="verify the file, e.g. sha256:ab12..")
    p.add_argument("--overwrite", action="store_true", help="replace an existing file, restart download")


def _add_server_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-s", "--server", metavar="URL",
                   help=f"server/agent URL (default: $PYWGET_SERVER or http://{DEFAULT_HOST}:{DEFAULT_PORT})")
    p.add_argument("--token", metavar="TOKEN", help="API token (default: $PYWGET_TOKEN or the token file)")
    p.add_argument("--server-no-check-certificate", action="store_true", help="skip TLS verification of the server")
    p.add_argument("--server-ca-cert", metavar="FILE", help="CA bundle for the server certificate")


def _parse_headers(items: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"invalid header (expected 'Name: value'): {item!r}")
        headers[name.strip()] = value.strip()
    return headers


def _parse_cookie(text: Optional[str]) -> Optional[Any]:
    if text and text.lstrip().startswith("{"):
        try:
            value = json.loads(text)
        except ValueError:
            raise ValueError("--cookie looks like JSON but is invalid") from None
        if not isinstance(value, dict):
            raise ValueError("--cookie JSON must be an object")
        return value
    return text


def _password(args: argparse.Namespace) -> Optional[str]:
    password = args.password or os.environ.get("PYWGET_PASSWORD")
    if args.user and password is None and sys.stdin.isatty():
        password = getpass.getpass(f"Password for {args.user}: ")
    return password


def _expand_inputs(items: List[str], input_file: Optional[str] = None) -> List[str]:
    """URLs, 'c' (clipboard), existing text files of links, and -i FILE."""
    urls: List[str] = []

    def from_text(text: str) -> None:
        urls.extend(line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))

    for item in items:
        if item.lower() == "c":
            found = extract_urls(read_clipboard())
            if not found:
                raise PywgetError("no URL found in the clipboard")
            urls.extend(found)
        elif "://" not in item and Path(item).is_file():
            from_text(Path(item).read_text(encoding="utf-8"))
        else:
            urls.append(item)
    if input_file:
        from_text(sys.stdin.read() if input_file == "-" else Path(input_file).read_text(encoding="utf-8"))
    return urls


def _on_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _install_signals() -> None:
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="pywget: %(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _client(args: argparse.Namespace) -> ServerClient:
    return ServerClient(
        args.server, args.token,
        verify=not args.server_no_check_certificate, ca_file=args.server_ca_cert,
    )


# ------------------------------------------------------------------ config file


def _bool(value: str) -> bool:
    low = value.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


Spec = Dict[str, Tuple[str, Callable[[str], Any]]]

DOWNLOAD_SPEC: Spec = {
    "path": ("path", str), "connections": ("connections", int), "retries": ("retries", int),
    "timeout": ("timeout", float), "limit_rate": ("limit_rate", str), "user_agent": ("user_agent", str),
    "proxy": ("proxy", str), "ca_cert": ("ca_cert", str),
}
CLIENT_SPEC: Spec = {
    "server": ("server", str), "verify": ("server_no_check_certificate", lambda v: not _bool(v)),
    "ca_cert": ("server_ca_cert", str),
}
_COMMON_SERVER: Spec = {
    "host": ("host", str), "port": ("port", int), "state_dir": ("state_dir", str),
    "max_jobs": ("max_jobs", int), "allowed_host": ("allowed_host", _list),
    "tls_cert": ("tls_cert", str), "tls_key": ("tls_key", str), "ui": ("no_ui", lambda v: not _bool(v)),
}
SERVE_SPEC: Spec = dict(_COMMON_SERVER, **{
    "root": ("root", str), "connections": ("connections", int), "retries": ("retries", int),
    "limit_rate": ("limit_rate", str), "deny_private_networks": ("deny_private_networks", _bool),
})
AGENT_SPEC: Spec = dict(_COMMON_SERVER, **{
    "upstream": ("upstream", str), "upstream_token": ("upstream_token", str),
    "upstream_verify": ("upstream_no_check_certificate", lambda v: not _bool(v)),
    "upstream_ca_cert": ("upstream_ca", str),
})


def _apply_config(parser: argparse.ArgumentParser, section: str, spec: Spec) -> None:
    """Make config-file values the parser defaults (command line still wins)."""
    values = load_config().get(section, {})
    unknown = sorted(set(values) - set(spec))
    if unknown:
        raise ConfigError(
            f"unknown option(s) in [{section}] of {config_file_path()}: {', '.join(unknown)}"
        )
    defaults: Dict[str, Any] = {}
    for key, raw in values.items():
        dest, convert = spec[key]
        if raw == "":
            continue
        try:
            defaults[dest] = convert(raw)
        except ValueError:
            raise ConfigError(f"invalid value for [{section}] {key} in {config_file_path()}: {raw!r}") from None
    parser.set_defaults(**defaults)


def _config_main(argv: List[str]) -> int:
    try:
        from rchf import CustomRichHelpFormatter
    except:
        CustomRichHelpFormatter = argparse.RawDescriptionHelpFormatter
    
    p = argparse.ArgumentParser(prog="pywget config", description="Manage the optional config file.", formatter_class=CustomRichHelpFormatter)
    p.add_argument("action", choices=["path", "init", "show"])
    p.add_argument("--force", action="store_true", help="init: overwrite an existing file")
    args = p.parse_args(argv)
    path = config_file_path()
    if args.action == "path":
        print(path)
    elif args.action == "init":
        print(write_default_config(args.force))
    else:
        if not path.exists():
            print(f"(no config file; create one with: pywget config init)  [{path}]", file=sys.stderr)
            return 0
        shown = []
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip().lower()
            shown.append(f"{key} = ********" if key == "upstream_token" and "=" in line else line)
        print("\n".join(shown))
    return 0


def _token_main(argv: List[str]) -> int:
    try:
        from rchf import CustomRichHelpFormatter
    except:
        CustomRichHelpFormatter = argparse.RawDescriptionHelpFormatter
    
    p = argparse.ArgumentParser(
        prog="pywget token",
        description="Manage the API token (stored in the token file, mode 0600).",
        formatter_class=CustomRichHelpFormatter
    )
    p.add_argument("action", choices=["show", "new", "set", "path"])
    p.add_argument("value", nargs="?", help="set: the token to use ('-' = read from stdin)")
    p.add_argument("--length", type=int, default=32, metavar="BYTES", help="new: random bytes (default 32)")
    args = p.parse_args(argv)
    if args.action == "path":
        print(token_path())
        return 0
    if args.action == "show":
        token, source = resolve_token()
        if not token:
            print("no token yet: it is generated the first time you run 'pywget serve' (or run 'pywget token new')",
                  file=sys.stderr)
            return 1
        print(f"token source: {source}", file=sys.stderr)
        print(token)
        return 0
    if args.action == "new":
        token = generate_token(args.length)
    else:
        if not args.value:
            p.error("'set' needs a value (or '-' to read stdin)")
        token = (sys.stdin.readline() if args.value == "-" else args.value).strip()
        validate_token(token)
    path = save_token(token)
    print(token)
    print(f"saved to {path}; restart 'pywget serve' / 'pywget agent' to apply it", file=sys.stderr)
    return 0


# ------------------------------------------------------------- direct download


def build_parser() -> argparse.ArgumentParser:
    try:
        from rchf import CustomRichHelpFormatter
    except:
        CustomRichHelpFormatter = argparse.RawDescriptionHelpFormatter

    p = argparse.ArgumentParser(
        prog="pywget",
        description="Robust, resumable, multi-connection wget alternative with server and agent modes.",
        epilog=EPILOG,
        formatter_class=CustomRichHelpFormatter,
    )
    p.add_argument("urls", nargs="*", metavar="URL", help="http(s) URLs, 'c' for the clipboard, or a file of links")
    p.add_argument("-i", "--input-file", metavar="FILE", help="read URLs from FILE ('-' = stdin)")
    p.add_argument("-p", "-P", "--path", "--directory-prefix", dest="path", default=".", metavar="DIR",
                   help="download directory (default: .); with --server: folder below the server root")
    p.add_argument("-O", "--output", metavar="NAME", help="output file name (single URL only)")
    p.add_argument("-t", "--retries", type=int, default=5, metavar="N", help="retries per request/segment (default: 5)")
    p.add_argument("-T", "--timeout", type=float, default=30.0, metavar="SEC", help="network timeout (default: 30)")
    _add_request_args(p, local=True)
    p.add_argument("--proxy", metavar="URL", help="proxy URL (default: environment)")
    p.add_argument("--no-check-certificate", action="store_true", help="skip TLS verification of the download")
    p.add_argument("--ca-cert", metavar="FILE", help="custom CA bundle for the download")
    p.add_argument("--no-resume", action="store_true", help="never resume partial downloads")
    p.add_argument("-c", "--continue", dest="cont", action="store_true",
                   help="accepted for wget compatibility (resume is on by default)")
    p.add_argument("--no-mtime", action="store_true", help="do not copy Last-Modified to the file")
    p.add_argument("-a", "--add-only", action="store_true", help="with --server: queue paused, do not start")
    p.add_argument("--wait", action="store_true", help="with --server: wait until the jobs finish")
    _add_server_args(p)
    p.add_argument("-q", "--quiet", action="store_true", help="no progress or status output")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--json", action="store_true", help="print machine-readable results to stdout")
    p.add_argument("-V", "--version", action="version", version=f"pywget {__version__}")
    return p


def _build_options(args: argparse.Namespace, progress: Optional[ProgressBar]) -> Options:
    extra: Dict[str, Any] = {}
    if args.user_agent:
        extra["user_agent"] = args.user_agent
    return Options(
        output_dir=args.path,
        connections=args.connections or 4,
        retries=args.retries,
        timeout=args.timeout,
        limit_rate=args.limit_rate or 0,
        resume=not args.no_resume,
        overwrite=args.overwrite,
        preserve_mtime=not args.no_mtime,
        headers=build_headers(args.referrer, _parse_cookie(args.cookie), _parse_headers(args.header)),
        username=args.user,
        password=_password(args),
        proxy=args.proxy,
        verify_ssl=not args.no_check_certificate,
        ca_file=args.ca_cert,
        checksum=args.checksum,
        progress=progress,
        **extra,
    )


def _download_main(argv: List[str]) -> int:
    parser = build_parser()
    _apply_config(parser, "download", DOWNLOAD_SPEC)
    args = parser.parse_args(argv)
    try:
        urls = _expand_inputs(args.urls, args.input_file)
    except (OSError, PywgetError) as exc:
        parser.error(str(exc))
    if not urls:
        parser.error("no URLs given")
    if args.output and len(urls) > 1:
        parser.error("--output can only be used with a single URL")
    if (args.add_only or args.wait) and not args.server:
        parser.error("--add-only and --wait need --server")
    _setup_logging(args.verbose)
    _install_signals()

    if args.server:
        return _forward(args, urls, parser)

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
            note = ", skipped (already complete)" if result.skipped else ""
            note += ", resumed" if result.resumed else ""
            print(
                f"OK  {result.path}  ({format_size(result.size)}, {result.elapsed:.1f}s, "
                f"{format_size(result.speed)}/s, {result.connections} conn{note})",
                file=sys.stderr,
            )
    if args.json:
        print(json.dumps(results, indent=2))
    return exit_code


# ------------------------------------------------------------ server forwarding


def _job_specs(args: argparse.Namespace, urls: List[str]) -> List[Dict[str, Any]]:
    common: Dict[str, Any] = {
        "path": "" if args.path in (".", None) else args.path,
        "referrer": args.referrer,
        "cookie": _parse_cookie(args.cookie),
        "headers": _parse_headers(args.header) or None,
        "user_agent": args.user_agent,
        "username": args.user,
        "password": _password(args) if args.user else None,
        "connections": args.connections,
        "checksum": args.checksum,
        "limit_rate": args.limit_rate,
        "overwrite": True if args.overwrite else None,
        "add_only": True if args.add_only else None,
    }
    common = {k: v for k, v in common.items() if v not in (None, "")}
    specs = [dict(common, url=u) for u in urls]
    if args.output:
        specs[0]["filename"] = args.output
    return specs


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 2, 1)] + ".."


def _fmt_job(job: Dict[str, Any]) -> str:
    cols = shutil.get_terminal_size((100, 24)).columns
    pct = f"{job['percent']:5.1f}%" if job.get("percent") is not None else "   --%"
    speed = f"{format_size(job['speed'])}/s" if job.get("status") == "running" else ""
    prefix = f"{job['id']}  {job['status']:<9} {pct} {speed:>11}  "
    line = prefix + _clip(job.get("name") or job["url"], max(20, cols - len(prefix) - 1))
    if job.get("error"):
        line += "\n    ! " + _clip(job["error"], max(20, cols - 8))
    return line


def _forward(args: argparse.Namespace, urls: List[str], parser: argparse.ArgumentParser) -> int:
    try:
        client = _client(args)
        jobs = client.submit(_job_specs(args, urls))
    except ValueError as exc:
        parser.error(str(exc))
    except PywgetError as exc:
        print(f"pywget: error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 7)
    quiet = getattr(args, "quiet", False)
    if not quiet:
        for job in jobs:
            verb = "ADDED " if job["status"] == "paused" else "QUEUED"
            print(f"{verb} {job['id']}  {job['url']}", file=sys.stderr)
    code = 0
    if getattr(args, "wait", False):
        try:
            for job in jobs:
                last = [""]

                def show(j: Dict[str, Any], last: List[str] = last) -> None:
                    line = _fmt_job(j).split("\n")[0]
                    if not quiet and line != last[0] and sys.stderr.isatty():
                        sys.stderr.write("\r" + line + " " * max(0, len(last[0]) - len(line)))
                        sys.stderr.flush()
                    last[0] = line

                job = client.wait(job["id"], callback=show)
                if not quiet and sys.stderr.isatty():
                    sys.stderr.write("\n")
                if job["status"] != "completed":
                    print(f"pywget: job {job['id']} {job['status']}: {job.get('error') or ''}", file=sys.stderr)
                    code = code or 1
                elif not quiet:
                    print(f"OK  {job.get('result_path')}", file=sys.stderr)
                jobs[jobs.index(next(j for j in jobs if j["id"] == job["id"]))] = job
        except KeyboardInterrupt:
            print("\npywget: stopped waiting (jobs keep running on the server)", file=sys.stderr)
            return 130
        except PywgetError as exc:
            print(f"pywget: error: {exc}", file=sys.stderr)
            return getattr(exc, "exit_code", 7)
    if getattr(args, "json", False):
        print(json.dumps(jobs, indent=2))
    return code


def _send_main(argv: List[str]) -> int:
    try:
        from rchf import CustomRichHelpFormatter
    except:
        CustomRichHelpFormatter = argparse.RawDescriptionHelpFormatter
    
    p = argparse.ArgumentParser(prog="pywget send", description="Send links to a pywget server or agent.", formatter_class=CustomRichHelpFormatter)
    p.add_argument("urls", nargs="*", metavar="URL", help="http(s) URLs, 'c' for the clipboard, or a file of links")
    p.add_argument("-i", "--input-file", metavar="FILE", help="read URLs from FILE ('-' = stdin)")
    p.add_argument("-p", "--path", default=".", metavar="DIR", help="folder below the server root")
    p.add_argument("-O", "--output", metavar="NAME", help="file name (single URL only)")
    _add_request_args(p, local=False)
    p.add_argument("-a", "--add-only", action="store_true", help="queue paused, do not start downloading")
    p.add_argument("--wait", action="store_true", help="wait until the jobs finish")
    _add_server_args(p)
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true", help="print jobs as JSON on stdout")
    _apply_config(p, "client", CLIENT_SPEC)
    args = p.parse_args(argv)
    try:
        urls = _expand_inputs(args.urls, args.input_file)
    except (OSError, PywgetError) as exc:
        p.error(str(exc))
    if not urls:
        p.error("no URLs given")
    if args.output and len(urls) > 1:
        p.error("--output can only be used with a single URL")
    _setup_logging(args.verbose)
    _install_signals()
    return _forward(args, urls, p)


# --------------------------------------------------------------------- jobs


def _jobs_main(argv: List[str]) -> int:
    try:
        from rchf import CustomRichHelpFormatter
    except:
        CustomRichHelpFormatter = argparse.RawDescriptionHelpFormatter
    
    p = argparse.ArgumentParser(prog="pywget jobs", description="Inspect and control server jobs.", formatter_class=CustomRichHelpFormatter)
    p.add_argument("action", choices=["list", "show", "pause", "resume", "cancel", "retry", "remove", "stats"])
    p.add_argument("job_id", nargs="?", help="job id (not needed for list/stats)")
    p.add_argument("--status", help="filter for 'list' (queued, running, paused, completed, failed, cancelled)")
    p.add_argument("--json", action="store_true")
    _add_server_args(p)
    _apply_config(p, "client", CLIENT_SPEC)
    args = p.parse_args(argv)
    if args.action not in ("list", "stats") and not args.job_id:
        p.error(f"'{args.action}' needs a job id")
    try:
        client = _client(args)
        if args.action == "list":
            result: Any = client.list(args.status)
            text = "\n".join(_fmt_job(j) for j in result) or "(no jobs)"
        elif args.action == "stats":
            result = client.stats()
            text = json.dumps(result, indent=2)
        elif args.action == "show":
            result = client.get(args.job_id)
            text = json.dumps(result, indent=2)
        elif args.action == "remove":
            client.remove(args.job_id)
            result, text = {"removed": args.job_id}, f"removed {args.job_id}"
        else:
            result = client.action(args.job_id, args.action)
            text = _fmt_job(result)
    except PywgetError as exc:
        print(f"pywget: error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 7)
    print(json.dumps(result, indent=2) if args.json else text)
    return 0


# ------------------------------------------------------------- serve / agent


def _serve_main(argv: List[str], agent: bool) -> int:
    name = "agent" if agent else "serve"
    try:
        from rchf import CustomRichHelpFormatter
    except:
        CustomRichHelpFormatter = argparse.RawDescriptionHelpFormatter
    
    p = argparse.ArgumentParser(
        prog=f"pywget {name}",
        description=(
            "Run a relay that forwards submitted links to an upstream pywget server."
            if agent else
            "Run the pywget download server: HTTP API, persistent queue, web UI."
        ),
        formatter_class=CustomRichHelpFormatter
    )
    p.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default: {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_AGENT_PORT if agent else DEFAULT_PORT, help="TCP port")
    p.add_argument("--token", help="API token (default: $PYWGET_TOKEN, token file, or generated)")
    if agent:
        p.add_argument("--upstream", metavar="URL", help="server to forward links to (or 'upstream' in [agent])")
        p.add_argument("--upstream-token", metavar="TOKEN", help="token of the upstream server")
        p.add_argument("--upstream-no-check-certificate", action="store_true")
        p.add_argument("--upstream-ca-cert", metavar="FILE")
    else:
        p.add_argument("--root", default=None, metavar="DIR",
                       help="download root; jobs can only write below it (default: ~/Downloads/pywget)")
        p.add_argument("-n", "--connections", type=int, default=4, help="default connections per file")
        p.add_argument("-t", "--retries", type=int, default=5)
        p.add_argument("--limit-rate", metavar="RATE", help="global speed cap shared by all jobs")
        p.add_argument("--deny-private-networks", action="store_true",
                       help="refuse URLs that resolve to private/loopback/link-local addresses (SSRF guard)")
    p.add_argument("-j", "--max-jobs", type=int, default=32 if agent else 3, help="concurrent jobs")
    p.add_argument("--state-dir", metavar="DIR", help="where the job queue is persisted")
    p.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                   help="accepted Host header value (repeatable; DNS-rebinding protection)")
    p.add_argument("--tls-cert", metavar="FILE")
    p.add_argument("--tls-key", metavar="FILE")
    p.add_argument("--no-ui", action="store_true", help="disable the web UI")
    p.add_argument("-v", "--verbose", action="store_true")
    _apply_config(p, "agent" if agent else "serve", AGENT_SPEC if agent else SERVE_SPEC)
    args = p.parse_args(argv)
    if agent and not args.upstream:
        p.error("--upstream is required (or set 'upstream' in the [agent] section of the config file)")
    _setup_logging(args.verbose)
    _install_signals()
    try:
        token, token_source = resolve_token(args.token, create=True)
        kwargs: Dict[str, Any] = dict(
            host=args.host, port=args.port, token=token or "", token_source=token_source, max_jobs=args.max_jobs,
            allowed_hosts=args.allowed_host, tls_cert=args.tls_cert, tls_key=args.tls_key, ui=not args.no_ui,
        )
        if args.state_dir:
            kwargs["state_dir"] = Path(args.state_dir)
        elif agent:
            kwargs["state_dir"] = config_dir() / "agent"
        if agent:
            kwargs.update(
                upstream=args.upstream, upstream_token=args.upstream_token,
                upstream_verify=not args.upstream_no_check_certificate, upstream_ca=args.upstream_ca_cert,
            )
        else:
            kwargs.update(
                connections=args.connections, retries=args.retries, limit_rate=args.limit_rate or 0,
                deny_private=args.deny_private_networks,
            )
            if args.root:
                kwargs["root"] = Path(args.root)
        cfg = ServerConfig(**kwargs)
        from .server import run_server

        return run_server(cfg)
    except (ValueError, PywgetError) as exc:
        print(f"pywget: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"pywget: error: {exc}", file=sys.stderr)
        return 6


# ---------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        command, rest = args[0], args[1:]
        try:
            if command == "serve":
                return _serve_main(rest, agent=False)
            if command == "agent":
                return _serve_main(rest, agent=True)
            if command == "send":
                return _send_main(rest)
            if command == "token":
                return _token_main(rest)
            if command == "config":
                return _config_main(rest)
            return _jobs_main(rest)
        except ConfigError as exc:
            print(f"pywget: error: {exc}", file=sys.stderr)
            return exc.exit_code
    try:
        return _download_main(args)
    except ConfigError as exc:
        print(f"pywget: error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
