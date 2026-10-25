import pytest
from conftest import DATA

from pywget import Downloader, Options
from pywget.cli import _fmt_job, build_parser, main
from pywget.config import ServerConfig, load_config, resolve_token, token_path, validate_token
from pywget.errors import ConfigError, HTTPStatusError
from pywget.utils import short_url


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("PYWGET_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("PYWGET_TOKEN", raising=False)
    monkeypatch.delenv("PYWGET_CONFIG", raising=False)
    monkeypatch.delenv("PYWGET_SERVER", raising=False)
    return tmp_path / "cfg"


def test_token_auto_generated_once_and_reused(cfgdir):
    assert resolve_token() == (None, "")
    token, source = resolve_token(create=True)
    assert len(token) >= 40 and "generated" in source
    assert token_path().read_text().strip() == token
    assert resolve_token(create=True)[0] == token  # reused, not regenerated


def test_token_precedence(cfgdir, monkeypatch):
    file_token, _ = resolve_token(create=True)
    monkeypatch.setenv("PYWGET_TOKEN", "e" * 20)
    assert resolve_token()[0] == "e" * 20
    assert resolve_token("x" * 20)[0] == "x" * 20
    assert file_token not in ("e" * 20, "x" * 20)


@pytest.mark.parametrize("bad", ["short", "has space " * 3, "ünicode-" * 4, "a" * 300, ""])
def test_token_validation_rejects(bad):
    with pytest.raises(ConfigError):
        validate_token(bad)
    with pytest.raises(ValueError):
        ServerConfig(token=bad)


def test_token_accepts_urlsafe():
    assert validate_token("Abc-123_xyz.~+/=0123456789")


def test_token_cli(cfgdir, capsys):
    assert main(["token", "show"]) == 1
    assert main(["token", "new"]) == 0
    new = capsys.readouterr().out.strip().splitlines()[0]
    assert main(["token", "show"]) == 0
    assert capsys.readouterr().out.strip() == new
    assert main(["token", "set", "tooshort"]) == 2
    assert main(["token", "set", "my-own-secret-token-123"]) == 0
    assert token_path().read_text().strip() == "my-own-secret-token-123"
    assert main(["token", "path"]) == 0


def test_config_init_show_and_defaults(cfgdir, capsys):
    assert main(["config", "init"]) == 0
    assert (cfgdir / "config.ini").exists()
    assert main(["config", "init"]) == 2  # exists
    assert main(["config", "init", "--force"]) == 0
    assert all(not values for values in load_config().values())  # template is fully commented out
    path = cfgdir / "config.ini"
    path.write_text("[download]\nconnections = 2\nretries = 9\nlimit_rate = 1M\n[client]\nserver = http://nas:1\n"
                    "verify = false\n")
    from pywget.cli import CLIENT_SPEC, DOWNLOAD_SPEC, _apply_config

    parser = build_parser()
    _apply_config(parser, "download", DOWNLOAD_SPEC)
    args = parser.parse_args(["http://x/y"])
    assert (args.connections, args.retries, args.limit_rate) == (2, 9, "1M")
    assert parser.parse_args(["http://x/y", "-n", "7"]).connections == 7  # command line wins
    _apply_config(parser, "client", CLIENT_SPEC)
    args = parser.parse_args(["http://x/y"])
    assert args.server == "http://nas:1" and args.server_no_check_certificate is True
    assert main(["config", "show"]) == 0


def test_config_errors(cfgdir, capsys):
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.ini").write_text("[download]\nbogus = 1\n")
    assert main(["http://x/y", "-q"]) == 2
    assert "bogus" in capsys.readouterr().err
    (cfgdir / "config.ini").write_text("[download]\nconnections = many\n")
    assert main(["http://x/y", "-q"]) == 2
    (cfgdir / "config.ini").write_text("not an ini file")
    assert main(["http://x/y", "-q"]) == 2


def test_config_serve_section_and_agent_upstream(cfgdir, capsys):
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.ini").write_text("[serve]\nmax_jobs = 5\nui = false\nallowed_host = a, b\n")
    import argparse

    from pywget.cli import SERVE_SPEC, _apply_config
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-jobs", dest="max_jobs", type=int, default=3)
    parser.add_argument("--no-ui", dest="no_ui", action="store_true")
    parser.add_argument("--allowed-host", dest="allowed_host", action="append", default=[])
    _apply_config(parser, "serve", {k: v for k, v in SERVE_SPEC.items() if k in ("max_jobs", "ui", "allowed_host")})
    args = parser.parse_args([])
    assert (args.max_jobs, args.no_ui, args.allowed_host) == (5, True, ["a", "b"])


def test_agent_requires_upstream(cfgdir):
    with pytest.raises(SystemExit) as err:
        main(["agent"])
    assert err.value.code == 2


def test_short_url_hides_query_and_truncates():
    url = "https://nw28.seedr.cc/get_zip/123/" + "Very%20Long%20Name-" * 8 + ".zip?st=SECRET&e=1"
    short = short_url(url)
    assert "SECRET" not in short and len(short) <= 75 and short.endswith(".zip")
    assert short_url("http://h/file.bin") == "h/file.bin"


def test_http_error_message_has_no_query(server, tmp_path):
    with pytest.raises(HTTPStatusError) as err:
        Downloader(Options(output_dir=str(tmp_path), retries=0)).fetch(server + "/nope.zip?st=SECRET")
    assert "SECRET" not in str(err.value) and "nope.zip" in str(err.value)


def test_fmt_job_is_one_line_plus_error(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=(100, 24): __import__("os").terminal_size((100, 24)))
    job = {"id": "abc", "status": "failed", "percent": 24.3, "speed": 0, "url": "http://h/" + "x" * 500,
           "name": "n" * 500, "error": "boom " * 100}
    lines = _fmt_job(job).split("\n")
    assert len(lines) == 2 and all(len(line) <= 100 for line in lines)


def test_adaptive_connections_on_429(server, tmp_path):
    res = Downloader(Options(output_dir=str(tmp_path), connections=3, backoff=0.01, max_backoff=0.05,
                             retries=2)).fetch(server + "/limited")
    assert res.path.read_bytes() == DATA
