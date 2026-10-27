import http.client
import json
import time
from pathlib import Path

import pytest
from conftest import DATA, SMALL

from pywget.cli import main
from pywget.client import ServerClient
from pywget.config import ServerConfig
from pywget.errors import ServerError
from pywget.server import ServerHandle

TOKEN = "t" * 40


def make_cfg(tmp_path, **kw):
    kw.setdefault("root", tmp_path / "root")
    kw.setdefault("state_dir", tmp_path / "state")
    kw.setdefault("token", TOKEN)
    kw.setdefault("port", 0)
    kw.setdefault("retries", 2)
    return ServerConfig(**kw)


@pytest.fixture
def srv(tmp_path):
    with ServerHandle(make_cfg(tmp_path)) as handle:
        yield handle


def client_for(handle, token=TOKEN):
    return ServerClient(handle.url, token, timeout=10)


def wait_for(client, job_id, statuses, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} stuck at {job['status']}")


def test_health_open_api_needs_token(srv):
    assert client_for(srv, token="x").health()["status"] == "ok"
    with pytest.raises(ServerError) as err:
        client_for(srv, token="wrong").list()
    assert err.value.status == 401


def test_job_completes_and_secrets_hidden(srv, server):
    c = client_for(srv)
    job = c.submit({"url": server + "/small.txt", "path": "sub", "cookie": {"a": "b"},
                    "headers": {"X-Test": "1"}, "username": "u", "password": "p"})[0]
    assert "password" not in job and "cookie" not in job and job["has_auth"] and job["has_cookie"]
    done = wait_for(c, job["id"], {"completed", "failed"})
    assert done["status"] == "completed", done
    assert Path(done["result_path"]).read_bytes() == SMALL
    assert Path(done["result_path"]).parent == (srv.cfg.root / "sub").resolve()


@pytest.mark.parametrize("path", ["../escape", "/etc", "a/../../b"])
def test_path_traversal_rejected(srv, server, path):
    with pytest.raises(ServerError) as err:
        client_for(srv).submit({"url": server + "/small.txt", "path": path})
    assert err.value.status == 400


def test_validation_errors(srv, server):
    c = client_for(srv)
    for bad in ({"url": "ftp://x/y"}, {"url": server + "/x", "bogus": 1}, {"url": server + "/x", "connections": 99},
                {"url": server + "/x", "headers": {"A\r\nB": "x"}}, {}):
        with pytest.raises(ServerError) as err:
            c.submit(bad)
        assert err.value.status == 400, bad


def test_add_only_then_resume(srv, server):
    c = client_for(srv)
    job = c.submit({"url": server + "/small.txt", "add_only": True})[0]
    assert job["status"] == "paused"
    time.sleep(0.5)
    assert c.get(job["id"])["status"] == "paused"
    c.action(job["id"], "resume")
    assert wait_for(c, job["id"], {"completed", "failed"})["status"] == "completed"


def test_pause_resume_cancel_running(tmp_path, server):
    cfg = make_cfg(tmp_path, limit_rate="2M", connections=2)
    with ServerHandle(cfg) as handle:
        c = client_for(handle)
        job = c.submit({"url": server + "/big.bin"})[0]
        wait_for(c, job["id"], {"running"})
        deadline = time.time() + 10
        while c.get(job["id"])["downloaded"] < 500_000 and time.time() < deadline:
            time.sleep(0.1)
        c.action(job["id"], "pause")
        paused = wait_for(c, job["id"], {"paused"})
        assert paused["downloaded"] > 0
        assert list((cfg.root).glob("*.part"))
        c.action(job["id"], "resume")
        done = wait_for(c, job["id"], {"completed", "failed"}, timeout=40)
        assert done["status"] == "completed", done
        assert Path(done["result_path"]).read_bytes() == DATA
        c.remove(job["id"])
        assert c.list() == []

        job2 = c.submit({"url": server + "/big.bin", "filename": "second.bin"})[0]
        wait_for(c, job2["id"], {"running"})
        c.action(job2["id"], "cancel")
        assert wait_for(c, job2["id"], {"cancelled"})["status"] == "cancelled"
        assert not list(cfg.root.glob("second.bin*"))


def test_failed_job_and_retry(srv, server):
    c = client_for(srv)
    job = c.submit({"url": server + "/nope"})[0]
    failed = wait_for(c, job["id"], {"failed"})
    assert "404" in failed["error"]
    c.action(job["id"], "retry")
    assert wait_for(c, job["id"], {"failed"})["status"] == "failed"


def test_state_errors(srv, server):
    c = client_for(srv)
    with pytest.raises(ServerError) as err:
        c.get("doesnotexist")
    assert err.value.status == 404
    job = c.submit({"url": server + "/small.txt"})[0]
    done = wait_for(c, job["id"], {"completed"})
    with pytest.raises(ServerError) as err:
        c.action(done["id"], "pause")
    assert err.value.status == 409


def test_host_header_protection(srv):
    host, port = srv.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/health", headers={"Host": "evil.example.com"})
    assert conn.getresponse().status == 421
    conn.close()


def test_no_cors_and_oversize_body(srv):
    host, port = srv.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("OPTIONS", "/api/v1/jobs", headers={"Origin": "http://evil", "Access-Control-Request-Method": "POST"})
    resp = conn.getresponse()
    assert resp.status == 204 and resp.getheader("Access-Control-Allow-Origin") is None
    resp.read()
    conn.request("POST", "/api/v1/jobs", body=b"x" * 10, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Length": str(2 * 1024 * 1024)})
    assert conn.getresponse().status == 413
    conn.close()


def test_deny_private_networks(tmp_path, server):
    with ServerHandle(make_cfg(tmp_path, deny_private=True)) as handle:
        with pytest.raises(ServerError) as err:
            client_for(handle).submit({"url": server + "/small.txt"})
        assert err.value.status == 403


def test_queue_persists_across_restart(tmp_path, server):
    cfg = make_cfg(tmp_path)
    with ServerHandle(cfg) as handle:
        job = client_for(handle).submit({"url": server + "/small.txt", "add_only": True})[0]
    with ServerHandle(make_cfg(tmp_path)) as handle:
        again = client_for(handle).get(job["id"])
        assert again["status"] == "paused"
        client_for(handle).action(job["id"], "resume")
        assert wait_for(client_for(handle), job["id"], {"completed"})["status"] == "completed"


def test_web_ui_and_stats(srv):
    host, port = srv.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    assert resp.status == 200 and b"pywget" in resp.read()
    assert "default-src 'none'" in resp.getheader("Content-Security-Policy")
    conn.close()
    assert client_for(srv).stats()["mode"] == "server"


def test_events_stream(srv, server):
    host, port = srv.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/api/v1/events", headers={"Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    assert resp.status == 200
    client_for(srv).submit({"url": server + "/small.txt"})
    seen = b""
    deadline = time.time() + 10
    while b"event: job" not in seen and time.time() < deadline:
        seen += resp.fp.readline()
    assert b"event: job" in seen
    conn.close()


def test_agent_relays_to_upstream(tmp_path, server):
    up_cfg = make_cfg(tmp_path / "up")
    with ServerHandle(up_cfg) as upstream:
        agent_cfg = ServerConfig(
            port=0, token="a" * 40, state_dir=tmp_path / "agent", upstream=upstream.url,
            upstream_token=TOKEN, max_jobs=4,
        )
        with ServerHandle(agent_cfg) as agent:
            assert client_for(agent, "a" * 40).stats()["mode"] == "agent"
            ac = client_for(agent, "a" * 40)
            job = ac.submit({"url": server + "/small.txt", "path": "via-agent"})[0]
            done = wait_for(ac, job["id"], {"completed", "failed"})
            assert done["status"] == "completed", done
            assert (up_cfg.root / "via-agent" / "small.txt").read_bytes() == SMALL
            assert done["remote_status"] == "completed"


def test_agent_waits_for_offline_upstream(tmp_path, server):
    up_cfg = make_cfg(tmp_path / "up")
    upstream = ServerHandle(up_cfg)
    url = upstream.url
    upstream.httpd.server_close()  # port bound but nothing serving: connection refused soon
    agent_cfg = ServerConfig(port=0, token="a" * 40, state_dir=tmp_path / "agent", upstream=url,
                             upstream_token=TOKEN)
    with ServerHandle(agent_cfg) as agent:
        ac = client_for(agent, "a" * 40)
        job = ac.submit({"url": server + "/small.txt"})[0]
        # Windows needs ~2s per refused connection attempt, so poll instead of a fixed sleep
        deadline = time.time() + 20
        current = ac.get(job["id"])
        while time.time() < deadline and "upstream unavailable" not in (current["error"] or ""):
            time.sleep(0.2)
            current = ac.get(job["id"])
        assert current["status"] == "running" and "upstream unavailable" in (current["error"] or "")
        ac.action(job["id"], "cancel")
        assert wait_for(ac, job["id"], {"cancelled"})["status"] == "cancelled"


def test_cli_send_jobs_and_legacy_forward(srv, server, capsys, monkeypatch):
    monkeypatch.setenv("PYWGET_TOKEN", TOKEN)
    base = ["--server", srv.url]
    assert main(["send", server + "/small.txt", "-p", "cli", "--wait", "-q", "--json", *base]) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert jobs[0]["status"] == "completed"
    assert (srv.cfg.root / "cli" / "small.txt").read_bytes() == SMALL
    assert main([server + "/cd", "--server", srv.url, "--wait", "-q"]) == 0
    assert (srv.cfg.root / "report final.txt").exists()
    assert main(["jobs", "list", "--json", *base]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(["jobs", "show", jobs[0]["id"], *base]) == 0
    assert main(["jobs", "remove", jobs[0]["id"], *base]) == 0
    assert main(["jobs", "show", jobs[0]["id"], *base]) == 7
    assert main(["send", server + "/small.txt", "-a", "-q", *base]) == 0


def test_cli_send_url_file_and_bad_token(srv, server, tmp_path, monkeypatch):
    listing = tmp_path / "links.txt"
    listing.write_text(f"# links\n{server}/small.txt\n{server}/cd\n")
    assert main(["send", str(listing), "-q", "--server", srv.url, "--token", TOKEN, "--wait"]) == 0
    assert main(["send", server + "/small.txt", "-q", "--server", srv.url, "--token", "bad"]) == 7


def test_stop_reason_cleanup_helpers():
    from pywget.clipboard import extract_urls
    from pywget.utils import build_headers

    assert extract_urls("see (https://a.com/x.zip), and http://b.org/y") == ["https://a.com/x.zip", "http://b.org/y"]
    assert build_headers("http://r/", {"a": "b", "c": "d"}, {"X": "1"}) == {"X": "1", "Referer": "http://r/", "Cookie": "a=b; c=d"}
    with pytest.raises(ValueError):
        build_headers(headers={"X": "a\r\nInjected: 1"})


def test_watch_and_wait_all(srv, server):
    c = client_for(srv)
    ids = [j["id"] for j in c.submit([{"url": server + "/small.txt"}, {"url": server + "/nope"}])]
    last = {}
    for job in c.watch(ids, interval=0.2, timeout=20):
        last[job["id"]] = job["status"]
    assert sorted(last.values()) == ["completed", "failed"]
    seen = []
    finals = c.wait_all(ids, interval=0.2, timeout=20, callback=seen.append)
    assert [j["id"] for j in finals] == ids and seen  # same order as requested


def test_watch_all_ends_when_idle_and_times_out(srv, server):
    c = client_for(srv)
    job = c.submit({"url": server + "/small.txt", "add_only": True})[0]
    assert [j["id"] for j in c.watch(interval=0.2, timeout=5)] == [job["id"]]  # paused is not active
    with pytest.raises(ServerError):
        list(c.watch([job["id"]], interval=0.2, timeout=0.5))  # never reaches a terminal state


def test_events_client(srv, server):
    import threading

    c = client_for(srv)
    threading.Timer(0.3, lambda: c.submit({"url": server + "/small.txt"})).start()
    deadline = time.time() + 15
    statuses = []
    for event in c.events(timeout=10):
        if event.get("event") == "job":
            statuses.append(event["job"]["status"])
            if event["job"]["status"] == "completed":
                break
        assert time.time() < deadline
    assert "completed" in statuses


def test_cli_jobs_watch(srv, server, capsys, monkeypatch):
    monkeypatch.setenv("PYWGET_TOKEN", TOKEN)
    c = client_for(srv)
    ok = c.submit({"url": server + "/small.txt"})[0]["id"]
    bad = c.submit({"url": server + "/nope"})[0]["id"]
    base = ["--server", srv.url, "--interval", "0.2", "--timeout", "20"]
    assert main(["jobs", "watch", ok, "--json", *base]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[-1]["status"] == "completed" and all(line["id"] == ok for line in lines)
    assert main(["jobs", "watch", bad, *base]) == 1
    assert "failed" in capsys.readouterr().out
    assert main(["jobs", "watch", *base]) in (0, 1)  # no ids: all jobs, ends when idle
