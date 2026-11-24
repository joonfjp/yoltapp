"""Semantic and hybrid search, `like`, diversify, facets, themes, and embedding coverage and outages."""

import re

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.support.contract import ContractClient
from tests.support.embedder import FakeEmbeddingServer
from tests.support.seed import TALKS, seed
from yolt import embed
from yolt.db import Database
from yolt.embeddings import EmbeddingRejected, EmbeddingService, EmbeddingUnavailable
from yolt.repo import themes as theme_repo
from yolt.server import create_app


@pytest.fixture
def server():
    return FakeEmbeddingServer()


@pytest.fixture
def catalogue(database_url):
    """Two ready runs, not yet embedded."""
    db = Database(database_url)
    try:
        db.save_settings({"embed_url": "http://embeddings.example/v1/embeddings"})
        return {video: seed(db, video) for video in TALKS}
    finally:
        db.close()


@pytest.fixture
def make_api(tmp_path, database_url, server):
    clients = []

    def build(*, embedded: bool = True):
        if embedded:
            db = Database(database_url)
            try:
                embed.embed_pending(db, EmbeddingService(db.settings, server.client_factory()))
            finally:
                db.close()
        app = create_app(tmp_path, database_url=database_url, start_worker=False, allowed_hosts=["testserver"],
                         client_factory=server.client_factory())
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return ContractClient(client)

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def api(catalogue, make_api):
    return make_api()


def titles(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [c["title"] for c in response.json()["data"]]


def problem(response, status) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def test_coverage_before_and_after_embedding(catalogue, make_api):
    before = make_api(embedded=False).get("/api/v1/").json()["embeddings"]
    assert before["query_embedding"] == "configured"
    assert before["vectors"]["clips"] == {"total": 8, "embedded": 0, "stale": 0}
    after = make_api().get("/api/v1/").json()["embeddings"]
    assert after["vectors"]["clips"]["embedded"] == 8 and after["vectors"]["passages"]["embedded"] == 8
    assert after["vectors"]["latest_embedded_at"]


def test_semantic_search_finds_meaning_keyword_search_misses(api):
    assert titles(api.get("/api/v1/clips?q=uncertainty story decade")) == []  # no passage has all three words
    found = api.get("/api/v1/clips?q=uncertainty story decade&search=semantic&limit=3").json()["data"]
    assert [c["title"] for c in found[:2]] == ["Jobs After AI #4", "Jobs After AI #3"]
    match = found[0]["match"]
    assert 0 < match["similarity"] <= 1 and match["passages"][0]["similarity"] is not None
    assert all(a["match"]["similarity"] >= b["match"]["similarity"] for a, b in zip(found, found[1:]))
    top = api.get("/api/v1/clips?q=will machines feel anything&search=semantic").json()["data"][0]
    assert top["text"] == "Machines may never feel anything at all."


def test_hybrid_merges_keyword_and_meaning(api):
    found = api.get("/api/v1/clips?q=jobs&search=hybrid&limit=50").json()["data"]
    keyword = [c for c in found if c["match"]["relevance"] is not None]
    assert len(keyword) == 3 and all(c["episode"]["title"] == "Jobs After AI" for c in keyword)
    assert all(c["match"]["fused"] > 0 for c in found)
    assert found[0]["match"]["relevance"] is not None  # matched by both lists, so it ranks first
    compact = api.get("/api/v1/clips?q=jobs&search=hybrid&view=compact").json()["data"][0]["match"]
    assert set(compact) == {"similarity", "relevance", "fused"}


def test_like_finds_neighbours_and_validates(api, catalogue):
    first = api.get(f"/api/v1/clips?run_id={catalogue['aaaaaaaaaaa']['run_id']}&sort=rank").json()["data"][0]
    near = api.get(f"/api/v1/clips?like={first['id']}").json()["data"]
    assert first["id"] not in [c["id"] for c in near] and near[0]["episode"]["title"] == "Minds and Machines"
    elsewhere = api.get(f"/api/v1/clips?like={first['id']}"
                        f"&exclude_episode_id={catalogue['aaaaaaaaaaa']['episode_id']}").json()["data"]
    assert {c["episode"]["title"] for c in elsewhere} == {"Jobs After AI"}
    assert problem(api.get("/api/v1/clips?like=999"), 422)["errors"][0]["code"] == "clip_not_found"
    assert problem(api.get(f"/api/v1/clips?like={first['id']}&q=x"), 400)["errors"][0]["parameter"] == "like"


def test_semantic_parameter_rules(api):
    def bad(query):
        return problem(api.get(f"/api/v1/clips?{query}"), 400)["errors"][0]["parameter"]

    assert bad("search=semantic") == "search"
    assert bad("q=jobs&min_similarity=0.5") == "min_similarity"
    assert bad("q=jobs&search=semantic&sort=-score") == "sort"
    assert bad("q=" + "x" * 301 + "&search=semantic") == "q"
    assert bad("q=jobs&diversify=true") == "diversify"
    assert bad("search=fuzzy&q=jobs") == "search"
    tight = api.get("/api/v1/clips?q=jobs vanish appear&search=semantic&min_similarity=0.6").json()["data"]
    assert [c["text"] for c in tight] == ["Some jobs vanish and new ones appear."]
    spread = api.get("/api/v1/clips?q=jobs&search=hybrid&max_per_episode=1").json()["data"]
    assert len(spread) == 2


def test_diversify_returns_one_reranked_page(api):
    page = api.get("/api/v1/clips?q=consciousness machines jobs&search=semantic&diversify=true&limit=4").json()
    assert len(page["data"]) == 4 and page["next_cursor"] is None
    assert {c["episode"]["title"] for c in page["data"]} == {"Minds and Machines", "Jobs After AI"}
    first = api.get("/api/v1/clips?q=jobs&search=semantic&limit=1").json()
    assert problem(api.get(f"/api/v1/clips?q=jobs&search=semantic&diversify=true&cursor={first['next_cursor']}"),
                   400)["errors"][0]["parameter"] == "cursor"


def test_query_embeddings_are_cached(api, server):
    for _ in range(3):
        api.get("/api/v1/clips?q=machines thinking&search=semantic")
    assert server.queries.count("Instruct: Given a search query, retrieve podcast clips relevant to the query\n"
                                "Query: machines thinking") == 1


def test_outages_degrade_to_503_while_keyword_and_like_keep_working(api, catalogue, server):
    api.get("/api/v1/clips?q=consciousness&search=semantic")  # cached from before the outage
    server.down = True
    down = problem(api.get("/api/v1/clips?q=something new&search=hybrid"), 503)
    assert down["type"].endswith("/embedding-unavailable")
    assert titles(api.get("/api/v1/clips?q=consciousness&search=semantic"))  # the cache still answers
    assert titles(api.get("/api/v1/clips?q=jobs"))
    clip = api.get("/api/v1/clips?limit=1").json()["data"][0]["id"]
    assert titles(api.get(f"/api/v1/clips?like={clip}"))

    cleared = api.put("/api/v1/settings", json={"embed_url": ""}).json()
    assert cleared["values"]["embed_url"] == cleared["defaults"]["embed_url"] == ""
    assert api.get("/api/v1/").json()["embeddings"]["query_embedding"] == "not_configured"
    assert "configured" in problem(api.get("/api/v1/clips?q=else&search=semantic"), 503)["detail"]


def test_changing_the_model_makes_every_vector_pending(api, database_url, server):
    api.put("/api/v1/settings", json={"embed_model": "other/model"})
    info = api.get("/api/v1/").json()["embeddings"]
    assert info["model"] == "other/model@1024" and info["vectors"]["clips"]["embedded"] == 0
    clip = api.get("/api/v1/clips?limit=1").json()["data"][0]["id"]
    assert problem(api.get(f"/api/v1/clips?like={clip}"), 422)["errors"][0]["code"] == "not_embedded"
    db = Database(database_url)
    try:
        done = embed.embed_pending(db, EmbeddingService(db.settings, server.client_factory()))
    finally:
        db.close()
    assert (done.clips, done.passages) == (8, 8)
    assert api.get("/api/v1/").json()["embeddings"]["vectors"]["clips"]["embedded"] == 8


def test_facets_count_what_clips_would_return(api, catalogue):
    facets = api.get("/api/v1/clips/facets?group_by=episode&q=jobs").json()
    assert facets["clip_count"] == 3 and facets["group_count"] == 1
    [group] = facets["groups"]
    assert group == {"key": catalogue["bbbbbbbbbbb"]["episode_id"], "label": "Jobs After AI", "clip_count": 3,
                     "unused_count": 3}
    assert api.get("/api/v1/clips/facets?group_by=episode").json()["clip_count"] == 8
    assert problem(api.get("/api/v1/clips/facets?group_by=episode&q=jobs&search=semantic"), 400)[
        "errors"][0]["parameter"] == "min_similarity"
    semantic = api.get("/api/v1/clips/facets?group_by=episode&q=jobs&search=hybrid&min_similarity=0.3"
                       "&sort=-unused").json()
    assert semantic["groups"][0]["label"] == "Jobs After AI"
    problem(api.get("/api/v1/clips/facets?group_by=podcast"), 400)
    problem(api.get("/api/v1/clips/facets"), 400)


def test_themes_cluster_clips_and_filter_them(api, database_url):
    themes = api.get("/api/v1/themes?k=2").json()
    assert themes["k"] == 2 and themes["clip_count"] == 8 and len(themes["data"]) == 2
    for theme in themes["data"]:
        assert re.fullmatch(r"k2-\d+-[0-9a-f]{4}", theme["id"])
        assert theme["unused_count"] == theme["clip_count"] and theme["coverage"] == {"compilation_count": 0,
                                                                                      "compilations": []}
        assert theme["representatives"] and theme["keywords"]
    seen = []
    for theme in themes["data"]:
        members = api.get(f"/api/v1/clips?theme={theme['id']}&view=compact&limit=50").json()["data"]
        assert len(members) == theme["clip_count"]
        assert theme["representatives"][0]["id"] in {c["id"] for c in members}
        seen += [c["id"] for c in members]
    assert sorted(seen) == sorted(c["id"] for c in api.get("/api/v1/clips?limit=50").json()["data"])  # a partition
    again = api.get("/api/v1/themes?k=2").json()
    assert [t["id"] for t in again["data"]] == [t["id"] for t in themes["data"]]  # deterministic
    assert api.get("/api/v1/themes?k=2&sort=coverage").status_code == 200

    assert problem(api.get("/api/v1/themes?k=50"), 400)["errors"][0]["parameter"] == "k"
    assert problem(api.get("/api/v1/clips?theme=nope"), 400)["errors"][0]["parameter"] == "theme"
    stale = themes["data"][0]["id"]
    with psycopg.connect(database_url) as conn:  # any vector changing makes a new clustering
        conn.execute("UPDATE clip_vectors SET embedded_at = now() WHERE clip_id = (SELECT min(clip_id) FROM clip_vectors)")
    assert problem(api.get(f"/api/v1/clips?theme={stale}"), 422)["errors"][0]["code"] == "theme_not_found"


def test_theme_ids_outlive_the_cache(api):
    """A listed ID resolves after a restart or an eviction, whatever its k, while the vectors are the same."""
    listed = api.get("/api/v1/themes?k=3").json()["data"]
    theme_repo._cache.clear()  # as after a restart, or after other k's pushed it out
    for theme in listed:
        members = api.get(f"/api/v1/clips?theme={theme['id']}&view=compact&limit=50").json()["data"]
        assert len(members) == theme["clip_count"]
    assert [t["id"] for t in api.get("/api/v1/themes?k=3").json()["data"]] == [t["id"] for t in listed]
    k, _, digest = listed[0]["id"].split("-")
    for unknown in (f"{k}-999999-{digest}", f"k1-1-{digest}", f"k9-1-{digest}"):  # no such theme; k1; k > clips
        assert problem(api.get(f"/api/v1/clips?theme={unknown}"), 422)["errors"][0]["code"] == "theme_not_found"


def test_the_idle_worker_clusters_the_default_themes_ahead(catalogue, database_url, tmp_path, server, make_api,
                                                          monkeypatch):
    """Once new vectors are in and the worker is idle, it clusters the themes the home page and Search ask for
    (k=12 and 20, or every clip in a smaller catalogue: 8 here), so GET /themes finds them ready."""
    from yolt.domain import themes as clustering
    from yolt.pipeline import Worker

    theme_repo._cache.clear()
    clustered = []
    real = clustering.cluster
    monkeypatch.setattr(clustering, "cluster", lambda x, k, **kw: clustered.append(k) or real(x, k, **kw))
    db = Database(database_url)
    worker = Worker(db, tmp_path, embeddings=EmbeddingService(db.settings, server.client_factory()), idle_seconds=0)
    try:
        with db.connect() as conn:  # nothing embedded yet: nothing to cluster
            assert theme_repo.warm(conn, worker.embeddings.pinned(db.settings())) == []
        assert worker._lock.acquire()
        worker.active = True
        worker._tick()  # embeds the backlog; there may be more, so no themes yet (each batch changes them)
        assert clustered == []
        worker._tick()  # the backlog is done: the themes are clustered
        assert clustered == [8]
        worker._tick()  # nothing new embedded: not again
        assert clustered == [8]
        listed = make_api(embedded=False).get("/api/v1/themes?k=8")
        assert listed.status_code == 200 and listed.json()["clip_count"] == 8
        assert clustered == [8]  # served from what the worker clustered
    finally:
        worker._lock.release()
        db.close()


def test_themes_need_vectors(catalogue, make_api):
    body = problem(make_api(embedded=False).get("/api/v1/themes"), 503)
    assert body["type"].endswith("/vectors-unavailable")


def test_semantic_search_needs_vectors_of_the_current_model(catalogue, make_api, server):
    api = make_api(embedded=False)
    for query in ("clips?q=jobs&search=semantic", "clips?q=jobs&search=hybrid",
                  "clips/facets?group_by=episode&q=jobs&search=hybrid&min_similarity=0.3",
                  "compilations?q=jobs&search=semantic", "clips?theme=k2-1-abcd"):
        assert problem(api.get(f"/api/v1/{query}"), 503)["type"].endswith("/vectors-unavailable"), query
    assert server.queries == []  # nothing to compare a query with, so none was embedded
    clip = api.get("/api/v1/clips?limit=1").json()["data"][0]["id"]
    assert problem(api.get(f"/api/v1/clips?like={clip}"), 422)["errors"][0]["code"] == "not_embedded"
    assert titles(api.get("/api/v1/clips?q=jobs"))  # keyword search keeps working


@pytest.fixture
def rejected(monkeypatch):
    """Which inputs the embedding server rejected, for this test only."""
    monkeypatch.setattr(embed, "_rejected", {})
    return embed._rejected


@pytest.mark.parametrize("how", ["rejects", "nans"])
def test_an_input_the_server_rejects_is_skipped_not_retried_forever(catalogue, database_url, server, rejected, how):
    getattr(server, how).add("automation")  # one clip (and its passage) of the eight
    db = Database(database_url)
    try:
        service = EmbeddingService(db.settings, server.client_factory())
        done = embed.embed_pending(db, service, limit=64)
        assert (done.clips, done.passages) == (7, 7)  # everything else got through
        assert sorted(kind for _, _, kind, _ in rejected) == ["clip_vectors", "passage_vectors"]
        calls = len(server.calls)
        assert embed.embed_pending(db, service, limit=64).total == 0
        assert len(server.calls) == calls  # the rejected ones aren't sent again
        with db.connect() as conn:
            status = conn.execute("SELECT last_error FROM embedding_status").fetchone()["last_error"]
        assert "rejected 2 item(s)" in status
        service.settings = lambda: {**db.settings(), "embed_model": "other/model"}  # new settings: tried again
        assert embed._skipped(service.config(), "clip_vectors") == []
    finally:
        db.close()


def test_a_server_rejecting_every_input_backs_off(catalogue, database_url, server, rejected):
    server.rejects.update({"so", "the", "jobs", "machines", "nobody", "automation", "but", "that"})
    db = Database(database_url)
    try:
        with pytest.raises(EmbeddingUnavailable, match="rejected all 8 inputs"):
            embed.embed_pending(db, EmbeddingService(db.settings, server.client_factory()), limit=64)
    finally:
        db.close()


def test_embedding_settings_and_test_button(api, server):
    view = api.get("/api/v1/settings").json()
    assert view["values"]["embed_url"] == "http://embeddings.example/v1/embeddings"
    assert view["values"]["embed_model"] == "Qwen/Qwen3-Embedding-8B"
    assert problem(api.put("/api/v1/settings", json={"embed_url": "gpu-box"}), 422)["errors"][0]["pointer"] == "/embed_url"
    ok = api.post("/api/v1/settings/test", json={"service": "embeddings", "url": "http://gpu:8098/v1/embeddings"}).json()
    assert ok == {"ok": True, "message": "Embeddings work (Qwen/Qwen3-Embedding-8B, 1024 dimensions)."}
    server.down = True
    failed = api.post("/api/v1/settings/test", json={"service": "embeddings", "url": "http://gpu:8098/v1/embeddings",
                                                     "model": "m"}).json()
    assert not failed["ok"] and "HTTPStatusError" in failed["message"]


def test_embed_command(catalogue, database_url, server, monkeypatch, capsys):
    monkeypatch.setenv("YOLT_DATABASE_URL", database_url)
    assert embed.main(client_factory=server.client_factory()) == 0
    assert "Done: 8 clips, 8 passages embedded." in capsys.readouterr().out
    assert embed.main(client_factory=server.client_factory()) == 0
    assert "Done: 0 clips, 0 passages embedded." in capsys.readouterr().out
    server.down = True
    with psycopg.connect(database_url) as conn:
        conn.execute("DELETE FROM passage_vectors")
    assert embed.main(client_factory=server.client_factory()) == 1


def test_all_zero_vectors_never_match_or_break_ranking(api, database_url, catalogue):
    """Cosine with a zero vector is NaN; nothing that ranks may return it (or 500)."""
    zero = "[" + ",".join(["0"] * 1024) + "]"
    with psycopg.connect(database_url) as conn:
        conn.execute("UPDATE passage_vectors SET embedding = %s::vector", (zero,))
        conn.execute("UPDATE clip_vectors SET embedding = %s::vector WHERE clip_id = "
                     "(SELECT min(clip_id) FROM clip_vectors)", (zero,))
    clip = api.get("/api/v1/clips?limit=50&sort=rank").json()["data"][-1]["id"]
    for query in ("q=jobs&search=semantic", "q=jobs&search=hybrid", f"like={clip}",
                  "q=jobs&search=semantic&diversify=true", "q=jobs&search=semantic&view=compact"):
        found = api.get(f"/api/v1/clips?{query}").json()["data"]
        assert all(c["match"]["similarity"] is None or -1 <= c["match"]["similarity"] <= 1 for c in found), query
    assert api.get("/api/v1/clips/facets?group_by=episode&q=jobs&search=semantic&min_similarity=0").status_code == 200
    themes = api.get("/api/v1/themes?k=2").json()
    assert themes["clip_count"] == 7  # the clip with only zero vectors isn't clustered into a junk theme
    assert "7 embedded clips" in problem(api.get("/api/v1/themes?k=8"), 400)["errors"][0]["message"]


@pytest.mark.parametrize("vectors", [[[0.1] * 3], [], [[0.1] * 1024, [0.1] * 1024]])
def test_vectors_of_the_wrong_size_or_number_are_a_rejection(vectors):
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]})

    service = EmbeddingService(lambda: {"embed_url": "http://gpu/v1/embeddings", "embed_model": ""},
                               lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EmbeddingRejected, match="1024-dimension"):
        service.embed(["one text"])


@pytest.mark.parametrize("url", [
    "/api/v1/clips/facets?group_by=episode&q=jobs&search=hybrid",  # facets need min_similarity
    "/api/v1/clips?q=jobs&search=semantic&sort=-score",
    "/api/v1/clips?q=jobs&search=semantic&diversify=true&cursor=abc",
])
def test_a_query_the_request_cant_use_isnt_embedded(api, server, url):
    assert problem(api.get(url), 400)
    assert server.queries == []


def test_a_server_that_refuses_every_input_skips_none_of_them(catalogue, database_url, server, rejected, monkeypatch):
    monkeypatch.setattr(server, "handler", lambda request: httpx.Response(400, json={"error": "unknown dimensions"}))
    db = Database(database_url)
    try:
        service = EmbeddingService(db.settings, server.client_factory())
        with pytest.raises(EmbeddingUnavailable, match="rejected all"):
            embed.embed_pending(db, service, limit=64)
        assert rejected == {}  # its settings, not the catalogue: once it's fixed, everything embeds
    finally:
        db.close()


def test_skipped_inputs_are_tried_again_under_other_embedding_settings(catalogue, database_url, server, rejected):
    server.rejects.add("automation")
    db = Database(database_url)
    try:
        embed.embed_pending(db, EmbeddingService(db.settings, server.client_factory()), limit=64)
        assert rejected
        other = {**db.settings(), "embed_model": "other-model"}
        embed.embed_pending(db, EmbeddingService(lambda: other, server.client_factory()), limit=0)
        assert not [key for key in rejected if key[1] != "other-model"]  # forgotten: that server gets a new try
    finally:
        db.close()
