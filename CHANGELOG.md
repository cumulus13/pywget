# Changelog

## 2.0.0

Complete rewrite. Same CLI (`pywget URL -p DIR`) and same library call
(`wget.download(url, dir)`), much stronger engine.

### Added
- Multi-connection segmented downloads (`-n`) with automatic fallback to a single stream.
- Crash-safe resume: `.part` + `.pywget` state file, validated against size/ETag/Last-Modified.
- Retries with exponential backoff + jitter, `Retry-After` support, per-segment retry budget.
- Checksum verification (`--checksum sha256:...`), atomic rename, disk-space pre-check.
- Bandwidth limiter, custom headers, basic auth (flag, URL or `PYWGET_PASSWORD`), proxy, custom CA.
- Safe redirects (http/https only, credentials stripped on cross-origin hop).
- Filename from `Content-Disposition`, path-traversal-proof sanitising.
- Batch mode (`-i FILE`), `--json` output, stable exit codes, SIGINT/SIGTERM-safe shutdown.
- Typed public API (`Downloader`, `Options`, `DownloadResult`), progress callbacks, test suite, CI, release workflow.

### Changed
- Python 3.8+ required; no third-party dependencies.
- Packaging moved to `pyproject.toml`; console script is now `pywget.cli:main`
  (`pywget.wget:usage` still works).

### Removed
- Python 2 support.
