"""`python -m yolt`: it only listens on loopback addresses its Host check can serve, and builds the app
(directly, or through the dev reloader) to accept requests for the address it listens on."""

import sys

import pytest

from yolt import __main__ as cli


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost"])
def test_the_host_it_listens_on_is_one_its_host_check_accepts(host):
    assert host in cli.allowed_hosts(host)
    assert {"127.0.0.1", "localhost"} <= set(cli.allowed_hosts(host))


@pytest.mark.parametrize("host", ["::1", "0.0.0.0", "192.0.2.5"])
def test_it_refuses_hosts_it_cant_serve_safely(host, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["yolt", "--host", host])
    assert cli.main() == 2
    assert "Refusing to listen" in capsys.readouterr().err


@pytest.fixture
def started(monkeypatch, tmp_path):
    """main() up to the point it would serve: what it built the app with, and how it would run it."""
    calls = {}
    monkeypatch.setenv("YOLT_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("YOLT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOLT_HOST", "127.0.0.1")  # --dev sets it for the reloader; restored after the test
    monkeypatch.setattr(cli, "create_app", lambda data_dir, **kw: calls.setdefault("app", kw) and "app")
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.setdefault("run", (app, kw)))
    monkeypatch.setattr(cli, "_logging", lambda: None)

    def start(*argv):
        monkeypatch.setattr(sys, "argv", ["yolt", *argv])
        assert cli.main() == 0
        return calls

    return start


def test_the_server_accepts_requests_for_the_address_it_listens_on(started):
    calls = started("--host", "127.0.0.2")
    assert calls["run"][1]["host"] == "127.0.0.2"
    assert "127.0.0.2" in calls["app"]["allowed_hosts"]


def test_dev_mode_passes_the_host_to_the_reloaded_app(started, monkeypatch):
    calls = started("--host", "127.0.0.3", "--dev")
    assert calls["run"][0] == "yolt.__main__:dev_app" and calls["run"][1]["host"] == "127.0.0.3"
    assert "app" not in calls  # the reloader builds it, in a fresh process
    cli.dev_app()  # what the reloader calls in its fresh process
    assert "127.0.0.3" in calls["app"]["allowed_hosts"] and calls["app"]["dev"] is True
