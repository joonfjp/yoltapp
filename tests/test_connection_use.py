"""Requests hold at most one pooled connection, and none while they wait on the embedding server."""

import contextlib

import pytest
from fastapi.testclient import TestClient

from tests.support.contract import ContractClient
from tests.support.embedder import FakeEmbeddingServer
from tests.support.seed import TALKS, seed
from yolt import db as db_module
from yolt import embed
from yolt.db import Database
from yolt.embeddings import EmbeddingService
from yolt.server import create_app

B = "/api/v1"


@pytest.fixture
def server():
    return FakeEmbeddingServer()


@pytest.fixture
def runs(database_url, server):
    db = Database(database_url)
    try:
        db.save_settings({"embed_url": "http://embeddings.example/v1/embeddings"})
        seeded = {video: seed(db, video) for video in TALKS}
        embed.embed_pending(db, EmbeddingService(db.settings, server.client_factory()))
        return seeded
    finally:
        db.close()


@pytest.fixture
def app_and_api(tmp_path, database_url, server, runs, monkeypatch):
    # Every Settings read goes to the database, so a read made while holding a connection would show.
    monkeypatch.setattr(db_module, "SETTINGS_TTL", -1.0)
    app = create_app(tmp_path, database_url=database_url, start_worker=False, allowed_hosts=["testserver"],
                     client_factory=server.client_factory())
    with TestClient(app) as client:
        db = app.state.db
        held = {"now": 0, "max": 0, "during_embed": []}
        connect = db.connect

        @contextlib.contextmanager
        def counting():
            with connect() as conn:
                held["now"] += 1
                held["max"] = max(held["max"], held["now"])
                try:
                    yield conn
                finally:
                    held["now"] -= 1

        monkeypatch.setattr(db, "connect", counting)
        handler = server.handler

        def recording(request):
            held["during_embed"].append(held["now"])
            return handler(request)

        monkeypatch.setattr(server, "handler", recording)
        yield app, ContractClient(client), held


@pytest.mark.parametrize("url", [
    f"{B}/clips?q=creativity+in+schools&search=semantic",
    f"{B}/clips?q=creativity+in+schools&search=hybrid",
    f"{B}/clips/facets?q=creativity+in+schools&search=semantic&group_by=episode&min_similarity=0.1",
    f"{B}/compilations?q=creativity+in+schools&search=semantic",
])
def test_semantic_query_is_embedded_before_the_connection_is_taken(app_and_api, url):
    _, api, held = app_and_api
    resp = api.get(url)
    assert resp.status_code == 200, resp.text
    assert held["during_embed"] == [0]  # embedded once, holding no connection
    assert held["max"] == 1


def test_embedding_outage_is_asked_once_and_still_a_503(app_and_api, server):
    _, api, held = app_and_api
    server.down = True
    resp = api.get(f"{B}/clips?q=creativity&search=semantic")
    assert resp.status_code == 503
    assert resp.json()["type"].endswith("embedding-unavailable")
    assert held["during_embed"] == [0]  # not asked again while holding the connection


@pytest.mark.parametrize("url", [f"{B}/runs", f"{B}/episodes", f"{B}/themes?k=2", f"{B}/subscriptions"])
def test_requests_never_hold_two_connections(app_and_api, url):
    _, api, held = app_and_api
    assert api.get(url).status_code == 200
    assert held["max"] == 1


def test_run_counts_its_own_embedded_clips(app_and_api, runs, database_url):
    _, api, _ = app_and_api
    video, other = list(runs)
    run = api.get(f"{B}/runs/{runs[video]['run_id']}").json()
    assert run["clip_count"] > 0
    assert run["embedded_clip_count"] == run["clip_count"]
    # A second run of the same video, not embedded yet: its own count is 0, the first run's is unchanged.
    db = Database(database_url)
    try:
        second = seed(db, video, instructions="only the jokes")
    finally:
        db.close()
    fresh = api.get(f"{B}/runs/{second['run_id']}").json()
    assert fresh["clip_count"] > 0 and fresh["embedded_clip_count"] == 0
    assert api.get(f"{B}/runs/{runs[video]['run_id']}").json()["embedded_clip_count"] == run["clip_count"]
    listed = {r["id"]: r for r in api.get(f"{B}/runs").json()["data"]}
    assert listed[second["run_id"]]["embedded_clip_count"] == 0
    assert listed[runs[other]["run_id"]]["embedded_clip_count"] == listed[runs[other]["run_id"]]["clip_count"]
