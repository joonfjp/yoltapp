from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.support.fakes import make_client, make_fake
from tests.support.pg import TempPostgres
from yolt.db import migrate


@pytest.fixture(scope="session")
def pg():
    server = TempPostgres().start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def migrated_template(pg) -> str:
    with psycopg.connect(pg.create("yolt_template"), row_factory=dict_row) as conn:
        migrate(conn)
    return "yolt_template"


@pytest.fixture
def database_url(pg, migrated_template) -> str:
    """A fresh, fully migrated database for one test."""
    return pg.clone(migrated_template)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Fake Parakeet, Highlightr, LLM and YouTube; see tests/support/fakes.py."""
    return make_fake(monkeypatch, tmp_path)


@pytest.fixture
def client(fake, tmp_path, database_url):
    """The app, with its worker running, on a fresh database."""
    yield from make_client(fake, tmp_path, database_url)


def pytest_sessionfinish(session, exitstatus):
    """A full run fails if any API operation in the spec went untested."""
    from tests.support import contract

    if not contract.COVERAGE:
        return
    missing = sorted(contract.all_operations() - set(contract.COVERAGE))
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        reporter.write_sep("-", "contract coverage")
        reporter.write_line(f"{len(contract.COVERAGE)}/{len(contract.all_operations())} operations exercised")
        if missing:
            reporter.write_line("not exercised: " + ", ".join(missing))
    full_run = not session.config.option.keyword and not session.config.option.markexpr and \
        session.config.args in ([], ["tests"])
    if full_run and missing and exitstatus == 0:
        session.exitstatus = 1
