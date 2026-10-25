"""Configuration helpers: config dir, API token, server configuration."""

from __future__ import annotations

import configparser
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import ConfigError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_AGENT_PORT = 8766
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def config_dir() -> Path:
    override = os.environ.get("PYWGET_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "pywget"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pywget"


def token_path() -> Path:
    return config_dir() / "token"


def config_file_path() -> Path:
    override = os.environ.get("PYWGET_CONFIG")
    return Path(override).expanduser() if override else config_dir() / "config.ini"


_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{16,256}")


def validate_token(token: str) -> str:
    """A token is 16-256 characters of A-Z a-z 0-9 . _ ~ + / = - (safe in HTTP headers)."""
    if not _TOKEN_RE.fullmatch(token or ""):
        raise ConfigError(
            "invalid token: use 16-256 characters from A-Z a-z 0-9 . _ ~ + / = - "
            "(generate one with: pywget token new)"
        )
    return token


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(max(16, nbytes))


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def save_token(token: str) -> Path:
    validate_token(token)
    path = token_path()
    _write_private(path, token + "\n")
    return path


def resolve_token(explicit: Optional[str] = None, create: bool = False) -> Tuple[Optional[str], str]:
    """Return (token, source). Order: explicit > $PYWGET_TOKEN > token file > generated."""
    if explicit:
        return explicit.strip(), "command line"
    env = os.environ.get("PYWGET_TOKEN")
    if env:
        return env.strip(), "$PYWGET_TOKEN"
    path = token_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text, str(path)
    except OSError:
        pass
    if not create:
        return None, ""
    token = generate_token()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:  # lost a race with another process
        return path.read_text(encoding="utf-8").strip(), str(path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token, f"{path} (generated)"


def load_token(explicit: Optional[str] = None, create: bool = False) -> Optional[str]:
    return resolve_token(explicit, create)[0]


DEFAULT_CONFIG = """\
# pywget configuration file (INI).
# Precedence: command line  >  environment  >  this file  >  built-in defaults.
# Everything below is commented out: uncomment and edit what you need.
# Booleans: true / false.  Lists: comma separated.  The API token is NOT stored here
# (see: pywget token show | new | set).

[download]
# options for direct downloads:  pywget URL
;path = ~/Downloads
;connections = 4
;retries = 5
;timeout = 30
;limit_rate = 2M
;user_agent =
;proxy =
;ca_cert =

[client]
# options for: pywget send / jobs   (the server to talk to)
;server = http://127.0.0.1:8765
;verify = true
;ca_cert =

[serve]
# options for: pywget serve
;host = 127.0.0.1
;port = 8765
;root = ~/Downloads/pywget
;state_dir =
;max_jobs = 3
;connections = 4
;retries = 5
;limit_rate =
;deny_private_networks = false
;allowed_host =
;tls_cert =
;tls_key =
;ui = true

[agent]
# options for: pywget agent
;host = 127.0.0.1
;port = 8766
;upstream = https://nas:8765
;upstream_token =
;upstream_verify = true
;upstream_ca_cert =
;max_jobs = 32
;state_dir =
;allowed_host =
;tls_cert =
;tls_key =
;ui = true
"""


def load_config() -> Dict[str, Dict[str, str]]:
    path = config_file_path()
    if not path.exists():
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except (configparser.Error, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from None
    return {name: dict(parser.items(name)) for name in parser.sections()}


def write_default_config(force: bool = False) -> Path:
    path = config_file_path()
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists (use --force to overwrite)")
    _write_private(path, DEFAULT_CONFIG)
    return path


@dataclass
class ServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str = ""
    root: Path = field(default_factory=lambda: Path.home() / "Downloads" / "pywget")
    state_dir: Path = field(default_factory=lambda: config_dir() / "server")
    max_jobs: int = 3
    connections: int = 4
    retries: int = 5
    limit_rate: Any = 0
    deny_private: bool = False
    allowed_hosts: List[str] = field(default_factory=list)
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None
    ui: bool = True
    upstream: Optional[str] = None
    upstream_token: Optional[str] = None
    upstream_verify: bool = True
    upstream_ca: Optional[str] = None
    token_source: str = ""

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()
        self.state_dir = Path(self.state_dir).expanduser()
        try:
            validate_token(self.token)
        except ConfigError as exc:
            raise ValueError(str(exc)) from None
        if not 1 <= self.max_jobs <= 256:
            raise ValueError("max_jobs must be between 1 and 256")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("--tls-cert and --tls-key must be given together")

    @property
    def is_agent(self) -> bool:
        return bool(self.upstream)
