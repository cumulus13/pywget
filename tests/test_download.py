#!/usr/bin/env python3

# File: tests/test_download.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-25
# Description: 
# License: MIT

import hashlib
import json
from pathlib import Path

import pytest
from conftest import DATA, NORANGE, SMALL

import pywget
from pywget import Downloader, Options
from pywget.cli import main
from pywget.errors import ChecksumError, HTTPStatusError, InvalidURLError


def make(tmp_path, **kw):
    kw.setdefault("backoff", 0.01)
    kw.setdefault("max_backoff", 0.05)
    return Downloader(Options(output_dir=str(tmp_path), **kw))


def test_single_connection(server, tmp_path):
    res = make(tmp_path, connections=1).fetch(server + "/small.txt")
    assert res.path.read_bytes() == SMALL
    assert not list(tmp_path.glob("*.part")) and not list(tmp_path.glob("*.pywget"))


def test_multi_connection(server, tmp_path):
    res = make(tmp_path, connections=4).fetch(server + "/big.bin")
    assert res.path.read_bytes() == DATA
    assert res.connections == 3  # 6 MiB // 2 MiB min segment


def test_no_range_support_streams(server, tmp_path):
    res = make(tmp_path, connections=4).fetch(server + "/noranges.bin")
    assert res.path.read_bytes() == NORANGE
    assert res.connections == 1


def test_resume_after_interrupt(server, tmp_path):
    def stop(info):
        if info.downloaded > 1_000_000:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        make(tmp_path, connections=2, limit_rate="2M", progress=stop).fetch(server + "/big.bin")
    assert (tmp_path / "big.bin.part").exists() and (tmp_path / "big.bin.pywget").exists()
    res = make(tmp_path, connections=2).fetch(server + "/big.bin")
    assert res.resumed and res.path.read_bytes() == DATA
    assert not (tmp_path / "big.bin.pywget").exists()


def test_checksum_ok_and_bad(server, tmp_path):
    good = "sha256:" + hashlib.sha256(SMALL).hexdigest()
    assert make(tmp_path, checksum=good).fetch(server + "/small.txt").checksum == good
    bad_dir = tmp_path / "bad"
    with pytest.raises(ChecksumError):
        make(bad_dir, checksum="sha256:" + "0" * 64).fetch(server + "/small.txt")
    assert list(bad_dir.iterdir()) == []


def test_skip_existing_then_unique_name(server, tmp_path):
    d = make(tmp_path, connections=1)
    d.fetch(server + "/small.txt")
    assert d.fetch(server + "/small.txt").skipped
    (tmp_path / "small.txt").write_text("different size content")
    res = d.fetch(server + "/small.txt")
    assert res.path.name == "small.txt.1" and res.path.read_bytes() == SMALL


def test_overwrite(server, tmp_path):
    (tmp_path / "small.txt").write_text("old")
    res = make(tmp_path, overwrite=True).fetch(server + "/small.txt")
    assert res.path.read_bytes() == SMALL and res.path.name == "small.txt"


def test_content_disposition_name(server, tmp_path):
    assert make(tmp_path).fetch(server + "/cd").path.name == "report final.txt"


def test_retry_on_503(server, tmp_path, flaky):
    res = make(tmp_path, retries=4).fetch(server + "/flaky")
    assert res.path.read_bytes() == SMALL


def test_retries_exhausted(server, tmp_path, flaky):
    with pytest.raises(HTTPStatusError) as err:
        make(tmp_path, retries=1).fetch(server + "/flaky")
    assert err.value.status == 503


def test_404(server, tmp_path):
    with pytest.raises(HTTPStatusError) as err:
        make(tmp_path).fetch(server + "/nope")
    assert err.value.status == 404


def test_redirect(server, tmp_path):
    assert make(tmp_path).fetch(server + "/redirect").path.read_bytes() == SMALL


def test_basic_auth(server, tmp_path):
    with pytest.raises(HTTPStatusError) as err:
        make(tmp_path).fetch(server + "/auth")
    assert err.value.status == 401
    assert make(tmp_path, username="user", password="secret").fetch(server + "/auth").path.exists()
    host = server.replace("http://", "http://user:secret@")
    assert make(tmp_path / "u").fetch(host + "/auth").path.exists()


@pytest.mark.parametrize("bad", ["ftp://example.com/x", "file:///etc/passwd", "not a url"])
def test_invalid_url(tmp_path, bad):
    with pytest.raises(InvalidURLError):
        make(tmp_path).fetch(bad)


def test_cancel_before_start_is_cleared(server, tmp_path):
    d = make(tmp_path)
    d.cancel()
    assert d.fetch(server + "/small.txt").path.exists()


def test_top_level_api_and_legacy(server, tmp_path):
    assert pywget.download(server + "/small.txt", tmp_path).read_bytes() == SMALL
    from pywget import wget

    out = wget.download(server + "/cd", str(tmp_path / "legacy"), bar=False)
    assert isinstance(out, str) and Path(out).read_bytes() == SMALL


def test_cli_ok_json_and_errors(server, tmp_path, capsys):
    assert main([server + "/small.txt", "-p", str(tmp_path), "-q", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["ok"] and data[0]["size"] == len(SMALL)
    assert main([server + "/nope", "-p", str(tmp_path), "-q"]) == 4
    assert main([server + "/small.txt", "-p", str(tmp_path / "c"), "-q", "--checksum", "sha256:" + "1" * 64]) == 5
    assert main(["ftp://x/y", "-p", str(tmp_path), "-q"]) == 2


def test_cli_input_file(server, tmp_path):
    listing = tmp_path / "urls.txt"
    listing.write_text(f"# comment\n{server}/small.txt\n\n{server}/cd\n")
    assert main(["-i", str(listing), "-p", str(tmp_path / "out"), "-q"]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["report final.txt", "small.txt"]
