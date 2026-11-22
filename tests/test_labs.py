"""Labs (yolt/labs): Thread, and switching labs off."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.support import seed as seeding
from tests.support.contract import ContractClient
from tests.support.embedder import FakeEmbeddingServer
from yolt import embed
from yolt.db import Database
from yolt.embeddings import EmbeddingService
from yolt.labs import thread
from yolt.server import create_app

B = "/api/v1"
# A third talk that retells one line of the first ("Machines may never feel anything at all."), so two
# episodes say the same thing.
REVISITED = ("Minds Revisited", "Pod Three", "2026-05-01",
             "Machines may never feel anything at all. Gardens need patient hands and good soil.")


@pytest.fixture
def server():
    return FakeEmbeddingServer()


@pytest.fixture
def catalogue(database_url, monkeypatch):
    monkeypatch.setitem(seeding.TALKS, "ccccccccccc", REVISITED)
    db = Database(database_url)
    try:
        db.save_settings({"embed_url": "http://embeddings.example/v1/embeddings"})
        return {video: seeding.seed(db, video) for video in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")}
    finally:
        db.close()


@pytest.fixture
def make_api(tmp_path, database_url, server):
    clients = []

    def build(*, embedded: bool = True, client_factory=None):
        if embedded:
            db = Database(database_url)
            try:
                embed.embed_pending(db, EmbeddingService(db.settings, server.client_factory()))
            finally:
                db.close()
        client = TestClient(create_app(tmp_path, database_url=database_url, start_worker=False,
                                       allowed_hosts=["testserver"],
                                       client_factory=client_factory or server.client_factory()))
        client.__enter__()
        clients.append(client)
        return ContractClient(client)

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def api(catalogue, make_api):
    return make_api()


def problem(response, status) -> dict:
    assert response.status_code == status, response.text
    return response.json()


# --- listing and switching off --------------------------------------------------

def test_labs_are_listed_new_and_on_by_default(api):
    labs = api.get(f"{B}/labs").json()["data"]
    assert [lab["id"] for lab in labs] == ["thread", "foryou"]
    assert all(lab["new"] and lab["enabled"] for lab in labs)


def test_a_lab_switched_off_answers_404_and_comes_back(api):
    assert api.put(f"{B}/settings", json={"labs_disabled": " thread"}).status_code == 200
    enabled = {lab["id"]: lab["enabled"] for lab in api.get(f"{B}/labs").json()["data"]}
    assert enabled == {"thread": False, "foryou": True}
    off = problem(api.get(f"{B}/labs/thread?q=machines"), 404)
    assert off["type"].endswith("/lab-disabled") and "labs_disabled" in off["detail"]
    assert api.put(f"{B}/settings", json={"labs_disabled": ""}).status_code == 200
    assert api.get(f"{B}/labs/thread?q=machines").status_code == 200


def test_labs_disabled_must_name_labs(api):
    bad = problem(api.put(f"{B}/settings", json={"labs_disabled": "thread,nope"}), 422)
    assert bad["errors"][0]["pointer"] == "/labs_disabled" and "nope" in bad["errors"][0]["message"]


def test_labs_need_vectors(catalogue, make_api):
    client = make_api(embedded=False)
    assert problem(client.get(f"{B}/labs/thread?q=machines"), 503)["type"].endswith("/vectors-unavailable")


# --- thread ---------------------------------------------------------------------

def test_thread_orders_matches_so_each_leads_into_the_next(api):
    found = api.get(f"{B}/labs/thread?q=machines feel anything consciousness&limit=4&max_per_episode=2"
                    "&target_duration_ms=30000").json()
    clips = found["clips"]
    assert len(clips) == 4 and len(found["hops"]) == 3 and all(h is not None for h in found["hops"])
    episodes = [c["episode"]["id"] for c in clips]
    assert max(episodes.count(e) for e in episodes) <= 2
    assert found["duration_ms"] == sum(c["duration_ms"] for c in clips) == found["review"]["duration_ms"]
    assert found["review"]["item_count"] == 4 and found["review"]["target_duration_ms"] == 30000
    assert all("match" in c for c in clips)
    best = api.get(f"{B}/clips?q=machines feel anything consciousness&search=hybrid&max_per_episode=2"
                   "&diversify=true&limit=4").json()["data"]
    assert clips[0]["id"] == best[0]["id"]  # the best match opens
    assert sorted(c["id"] for c in clips) == sorted(c["id"] for c in best)


def test_thread_validates(api):
    assert problem(api.get(f"{B}/labs/thread"), 400)["errors"][0]["parameter"] == "q"
    assert problem(api.get(f"{B}/labs/thread?q=x&limit=1"), 400)["errors"][0]["parameter"] == "limit"
    assert problem(api.get(f"{B}/labs/thread?q=x&usage=picked"), 400)["errors"][0]["parameter"] == "usage"


def test_chain_follows_the_nearest_next():
    v = [np.array([1.0, 0.0]), np.array([0.0, 1.0]), None, np.array([0.8, 0.6])]
    assert thread.chain(v) == [0, 3, 1, 2]
    assert thread.chain([None, np.array([1.0, 0.0])]) == [0, 1]
    assert thread.chain([]) == []


def test_thread_leaves_out_overlapping_clips():
    def clip(i, transcript, first, last):
        return {"id": i, "transcript_id": transcript, "words": {"first": first, "last": last}}

    kept = thread.without_overlaps([clip(1, 7, 10, 20), clip(2, 7, 20, 30), clip(3, 8, 10, 20), clip(4, 7, 21, 30)])
    assert [c["id"] for c in kept] == [1, 3, 4]

