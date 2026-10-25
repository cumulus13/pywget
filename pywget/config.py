"""Configuration helpers: config dir, API token, server configuration."""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

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


def load_token(explicit: Optional[str] = None, create: bool = False) -> Optional[str]:
    """Resolve the API token: explicit > $PYWGET_TOKEN > token file (> generate)."""
    if explicit:
        return explicit.strip()
    env = os.environ.get("PYWGET_TOKEN")
    if env:
        return env.strip()
    path = token_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    if not create:
        return None
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:  # lost a race with another process
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


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

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()
        self.state_dir = Path(self.state_dir).expanduser()
        if not self.token:
            raise ValueError("a non-empty API token is required")
        if not 1 <= self.max_jobs <= 256:
            raise ValueError("max_jobs must be between 1 and 256")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("--tls-cert and --tls-key must be given together")

    @property
    def is_agent(self) -> bool:
        return bool(self.upstream)
