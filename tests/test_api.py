"""The catalogue API over seeded data: search, paging, transcripts, context, and the conventions."""

import pathlib

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.support.contract import ContractClient
from tests.support.seed import TALKS, seed
from yolt import api as api_module
from yolt.db import PLATFORM, Database
from yolt.domain import cursor as cursors
from yolt.server import create_app

@pytest.fixture
def seeded(tmp_path, database_url):
    db = Database(database_url)
    try:
        ids = {video: seed(db, video) for video in TALKS}
    finally:
        db.close()
    app = create_app(tmp_path, database_url=database_url, start_worker=False, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        yield ContractClient(client), ids


def problem(response, status):
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    return response.json()


def test_service_info(seeded):
    api, _ = seeded
    info = api.get("/api/v1/").json()
    migrations = len(list((pathlib.Path(api_module.__file__).parent / "migrations").glob("*.sql")))
    assert (info["api_version"], info["schema_version"]) == (api_module.API_VERSION, migrations)
    assert info["queue"] == {"queued": 0, "processing": 0}
    assert info["embeddings"]["model"] == "Qwen/Qwen3-Embedding-8B@1024"
    assert info["embeddings"]["vectors"]["clips"] == {"total": 8, "embedded": 0, "stale": 0}


def test_unknown_and_repeated_parameters_are_400(seeded):
    api, _ = seeded
    body = problem(api.get("/api/v1/clips?usge=never"), 400)
    assert body["errors"][0]["code"] == "unknown_parameter" and body["errors"][0]["parameter"] == "usge"
    body = problem(api.get("/api/v1/clips?q=a&q=b"), 400)
    assert body["errors"][0]["parameter"] == "q"
    assert problem(api.get("/api/v1/clips?include=everything"), 400)["errors"][0]["parameter"] == "include"
    assert problem(api.get("/api/v1/runs/abc"), 400)["errors"][0]["parameter"] == "run_id"
    assert problem(api.get("/api/v1/clips?sort=-relevance"), 400)["errors"][0]["parameter"] == "sort"


def test_missing_things_are_404_problems(seeded):
    api, _ = seeded
    for path in ("/api/v1/runs/999", "/api/v1/episodes/999", "/api/v1/transcripts/999", "/api/v1/clips/999",
                 "/api/v1/clips/999/context", "/api/v1/shares/nope", "/api/v1/transcripts/999/text",
                 "/api/v1/transcripts/999/srt"):
        problem(api.get(path), 404)
    assert api.client.get("/api/v1/nothing").status_code == 404


def test_runs_list_filters_and_pages(seeded):
    api, ids = seeded
    newest_first = [r["id"] for r in api.get("/api/v1/runs").json()["data"]]
    assert newest_first == [ids["bbbbbbbbbbb"]["run_id"], ids["aaaaaaaaaaa"]["run_id"]]
    first = api.get("/api/v1/runs?limit=1").json()
    second = api.get(f"/api/v1/runs?limit=1&cursor={first['next_cursor']}").json()
    assert [first["data"][0]["id"], second["data"][0]["id"]] == newest_first and second["next_cursor"] is None
    episode = ids["aaaaaaaaaaa"]["episode_id"]
    assert [r["episode"]["id"] for r in api.get(f"/api/v1/runs?episode_id={episode}").json()["data"]] == [episode]
    assert api.get("/api/v1/runs?status=queued").json()["data"] == []
    assert len(api.get("/api/v1/runs?status=ready&status=failed").json()["data"]) == 2
    problem(api.get("/api/v1/runs?status=done"), 400)


def test_episodes_list_and_full_transcript_search(seeded):
    api, ids = seeded
    listed = api.get("/api/v1/episodes").json()["data"]
    assert [e["title"] for e in listed] == ["Jobs After AI", "Minds and Machines"]  # newest published first
    one = listed[1]
    assert one["podcast"] == "Pod One" and one["published_at"] == "2026-01-05" and one["clip_count"] == 4
    assert one["transcripts"][0]["source"] == "parakeet" and one["run_count"] == 1

    found = api.get("/api/v1/episodes?q=consciousness").json()["data"]
    assert [e["id"] for e in found] == [ids["aaaaaaaaaaa"]["episode_id"]]
    assert found[0]["match"]["transcript_id"] == ids["aaaaaaaaaaa"]["transcript_id"]
    assert any("«consciousness»" in s for s in found[0]["match"]["snippets"])
    assert api.get('/api/v1/episodes?q="hardest problem" -jobs').json()["data"][0]["title"] == "Minds and Machines"
    assert len(api.get("/api/v1/episodes?published_after=2026-02-01").json()["data"]) == 1
    pod = api.get(f"/api/v1/runs/{ids['bbbbbbbbbbb']['run_id']}").json()["episode"]
    assert pod["podcast_url"] == "https://youtube.com/@PodTwo" and pod["duration_ms"] == 600000

    page = api.get("/api/v1/episodes?limit=1").json()
    assert problem(api.get(f"/api/v1/episodes?limit=1&q=jobs&cursor={page['next_cursor']}"), 400)[
        "errors"][0]["parameter"] == "cursor"  # a cursor only works with the filters that made it


def test_transcript_text_words_and_ranges(seeded):
    api, ids = seeded
    tid = ids["aaaaaaaaaaa"]["transcript_id"]
    meta = api.get(f"/api/v1/transcripts/{tid}").json()
    text = api.get(f"/api/v1/transcripts/{tid}/text")
    assert text.text == TALKS["aaaaaaaaaaa"][3] and meta["text_bytes"] == len(text.content)
    part = api.get(f"/api/v1/transcripts/{tid}/text", headers={"Range": "bytes=0-6"})
    assert part.status_code == 206 and part.text == "So what" and part.headers["content-range"].startswith("bytes 0-6/")
    assert problem(api.get(f"/api/v1/transcripts/{tid}/text", headers={"Range": "bytes=99999-"}), 416)

    words = api.get(f"/api/v1/transcripts/{tid}/words?first=1&last=3").json()["words"]
    assert words == [{"seq": 1, "word": "So", "start_ms": 0, "end_ms": 400},
                     {"seq": 2, "word": "what", "start_ms": 500, "end_ms": 900},
                     {"seq": 3, "word": "is", "start_ms": 1000, "end_ms": 1400}]
    assert problem(api.get(f"/api/v1/transcripts/{tid}/words?first=3&last=1"), 422)["errors"][0]["code"] == "words_reversed"
    assert problem(api.get(f"/api/v1/transcripts/{tid}/words?first=1&last=9000"), 422)["errors"][0]["code"] == "word_span_too_large"
    count = meta["word_count"]
    assert problem(api.get(f"/api/v1/transcripts/{tid}/words?first={count}&last={count + 1}"), 422)[
        "errors"][0]["code"] == "words_missing"
    assert problem(api.get(f"/api/v1/transcripts/{tid}/words?first=1"), 400)["errors"][0]["parameter"] == "last"


def test_keyword_search_over_passages(seeded):
    api, ids = seeded
    found = api.get("/api/v1/clips?q=jobs").json()["data"]
    assert {c["episode"]["title"] for c in found} == {"Jobs After AI"}
    assert all(c["match"]["relevance"] > 0 and c["match"]["passages"] for c in found)
    assert found[0]["match"]["passages"][0]["text"] == found[0]["text"]
    texts = {c["text"] for c in api.get("/api/v1/clips?q=consciousness").json()["data"]}
    assert texts == {"So what is consciousness really?", "I think consciousness is the hardest problem."}
    assert len(api.get("/api/v1/clips?q=machines").json()["data"]) == 1
    assert api.get("/api/v1/clips?q=machines -feel").json()["data"] == []  # web-search exclusions
    only = api.get(f"/api/v1/clips?q=jobs&exclude_episode_id={ids['bbbbbbbbbbb']['episode_id']}").json()["data"]
    assert only == []


def test_clip_filters_sorts_and_views(seeded):
    api, ids = seeded
    every = api.get("/api/v1/clips?limit=50").json()["data"]
    assert len(every) == 8 and [c["score"] for c in every] == sorted((c["score"] for c in every), reverse=True)
    run_b = ids["bbbbbbbbbbb"]["run_id"]
    ranked = api.get(f"/api/v1/clips?run_id={run_b}&sort=rank").json()["data"]
    assert [c["rank"] for c in ranked] == [1, 2, 3, 4] and {c["run_id"] for c in ranked} == {run_b}
    assert api.get("/api/v1/clips?sort=-created&limit=1").json()["data"][0]["id"] == ranked[0]["id"]
    assert len(api.get("/api/v1/clips?max_per_episode=1").json()["data"]) == 2
    assert all(c["score"] >= 0.5 for c in api.get("/api/v1/clips?min_score=0.5").json()["data"])
    short = api.get("/api/v1/clips?max_duration_ms=3000").json()["data"]
    assert short and all(c["duration_ms"] <= 3000 for c in short)

    pick = [every[3]["id"], every[0]["id"]]
    batch = api.get(f"/api/v1/clips?id={pick[0]}&id={pick[1]}&exclude_id={pick[1]}").json()
    assert [c["id"] for c in batch["data"]] == [pick[0]] and batch["next_cursor"] is None
    assert problem(api.get(f"/api/v1/clips?id={pick[0]}&limit=5"), 400)["errors"][0]["parameter"] == "limit"

    compact = api.get("/api/v1/clips?view=compact&limit=500").json()["data"]  # ClipSummary: no text
    assert len(compact) == 8 and "text" not in compact[0]
    problem(api.get("/api/v1/clips?limit=500"), 400)
    problem(api.get("/api/v1/clips?view=compact&include=quotes"), 400)

    page1 = api.get("/api/v1/clips?limit=3").json()
    page2 = api.get(f"/api/v1/clips?limit=3&cursor={page1['next_cursor']}").json()
    page3 = api.get(f"/api/v1/clips?limit=3&cursor={page2['next_cursor']}").json()
    assert [c["id"] for c in page1["data"] + page2["data"] + page3["data"]] == [c["id"] for c in every]
    assert page3["next_cursor"] is None


def test_clip_includes_and_share(seeded):
    api, _ = seeded
    clip = api.get("/api/v1/clips?sort=-score&limit=1").json()["data"][0]
    full = api.get(f"/api/v1/clips/{clip['id']}?include=quotes,details").json()
    assert full["quotes"] == [{"ordinal": 1, "text": " ".join(full["text"].split()[:2]),
                               "words": {"first": full["words"]["first"], "last": full["words"]["first"] + 1}}]
    assert full["details"]["summary"] == full["text"].lower() and full["title"].endswith("#1")
    assert full["usage"] == {"compilations": 0, "revisions": 0, "rendered": 0, "last_rendered_at": None}
    slug = full["share_url"].rsplit("/", 1)[1]
    assert api.get(f"/api/v1/shares/{slug}?include=quotes").json()["quotes"] == full["quotes"]


def test_clip_context(seeded):
    api, ids = seeded
    clips = api.get(f"/api/v1/clips?run_id={ids['aaaaaaaaaaa']['run_id']}&sort=rank").json()["data"]
    first = api.get(f"/api/v1/clips/{clips[0]['id']}/context").json()
    assert first["before"] == "" and first["clip"] == "So what is consciousness really?"
    assert first["alignment"] == "exact" and not first["starts_mid_sentence"] and not first["follows_question"]
    second = api.get(f"/api/v1/clips/{clips[1]['id']}/context?before_words=3&after_words=2").json()
    assert second["before"] == "is consciousness really?" and second["follows_question"]
    assert second["after"] == "Machines may"
    assert second["clip"] == "I think consciousness is the hardest problem."


JSON = {"Content-Type": "application/json"}


def test_nul_and_unpaired_surrogates_are_refused_up_front(seeded):
    """PostgreSQL can't store NUL and UTF-8 can't encode a lone surrogate: either is a 400 or 422, never a 500."""
    api, ids = seeded
    for url, parameter in (("/api/v1/clips?q=%00", "q"), ("/api/v1/clips/facets?group_by=episode&theme=a%00", "theme"),
                           ("/api/v1/speakers?q=a%00b", "q"), ("/api/v1/episodes?q=%00", "q"),
                           ("/api/v1/compilations?q=%00", "q"), ("/api/v1/lookup?url=%00", "url"),
                           ("/api/v1/runs?status=ready&cursor=x%00", "cursor"), ("/api/v1/shares/x%00", "slug")):
        errors = problem(api.get(url), 400)["errors"]
        assert [(e["code"], e["parameter"]) for e in errors] == [("invalid_value", parameter)], url

    episode = ids["aaaaaaaaaaa"]["episode_id"]
    bodies = [  # JSON text, so it can spell lone surrogates
        ("POST", "/api/v1/runs", '{"url": "https://youtu.be/aaaaaaaaaaa", "instructions": "a\\u0000"}', ["/instructions"]),
        ("POST", "/api/v1/compilations",
         '{"title": "T\\ud800", "items": [{"clip_id": 1}], "metadata": {"note": ["ok", "\\u0000"], "k\\ud83d": 1}}',
         ["/title", "/metadata", "/metadata/note/1"]),
        ("POST", "/api/v1/compilations/1/revisions", '{"based_on": 1, "items": [{"clip_id": 1, "text": {"from": "\\u0000"}}]}',
         ["/items/0/text/from"]),
        ("PUT", f"/api/v1/episodes/{episode}/speakers", '{"speakers": [{"name": "Mia\\u0000", "role": "host"}]}',
         ["/speakers/0/name"]),
        ("PUT", "/api/v1/settings", '{"clip_instructions": "\\udfff", "max_clips": "5"}', ["/clip_instructions"]),
        ("POST", "/api/v1/settings/test", '{"service": "llm", "url": "http://x", "api_key": "\\u0000"}', ["/api_key"]),
        ("POST", "/api/v1/revisions/1/renders", '{"format": "long", "output_path": "/out/\\u0000", "duration_ms": 1}',
         ["/output_path"]),
        ("POST", "/api/v1/subscriptions", '{"url": "@pod\\u0000"}', ["/url"]),
        ("POST", "/api/v1/segments/review", '"\\u0000"', [""]),
    ]
    for method, url, text, pointers in bodies:
        errors = problem(api.request(method, url, content=text.encode(), headers=JSON), 422)["errors"]
        assert [(e["code"], e["pointer"]) for e in errors] == [("invalid_value", p) for p in pointers], url
    raw = api.post("/api/v1/compilations", content=b'{"title": "\xed\xa0\x80", "items": [{"clip_id": 1}]}', headers=JSON)
    assert [e["pointer"] for e in problem(raw, 422)["errors"]] == ["/title"]  # a surrogate in UTF-8 bytes
    assert api.get("/api/v1/settings").json()["values"]["max_clips"] == "8"  # nothing was saved
    assert api.client.get("/c/x%00").status_code == 200  # the share page is the UI shell, as for any unknown slug
    broken = api.post("/api/v1/compilations", content=b'{"title": ', headers=JSON)  # not JSON at all
    assert problem(broken, 400)["title"] == "The body is not valid JSON"


def test_settings_numbers_are_ascii_digits(seeded):
    api, _ = seeded
    body = problem(api.put("/api/v1/settings", json={
        "max_clips": "\u00b2", "min_clip_seconds": "\u2460", "max_clip_seconds": "\u0663\u0660",
        "candidates_per_window": "\u00b3", "subscription_check_hours": "9" * 5000, "subscription_min_minutes": "\uff15"}), 422)
    assert sorted(e["pointer"] for e in body["errors"]) == [
        "/candidates_per_window", "/max_clip_seconds", "/max_clips", "/min_clip_seconds", "/subscription_check_hours",
        "/subscription_min_minutes"]
    saved = api.put("/api/v1/settings", json={"subscription_check_hours": "0", "max_clips": "3"})
    assert saved.status_code == 200 and saved.json()["values"]["max_clips"] == "3"


def test_altered_cursors_are_400(seeded):
    """A cursor's fingerprint binds it to its filters, not its key, so an edited key is checked before SQL sees it."""
    api, _ = seeded
    for key in (["x", 1], [True, 1], [1, True], [1, 1e30], [1, 2 ** 63], [float("nan"), 1], [float("inf"), 1],
                [10 ** 400, 1], [1, "2"], [1], [1, 2, 3]):
        cursor = cursors.encode(key, {"q": None})
        assert problem(api.get(f"/api/v1/speakers?cursor={cursor}"), 400)["errors"][0]["parameter"] == "cursor", key
    assert api.get(f"/api/v1/speakers?cursor={cursors.encode([1, 1], {'q': None})}").json()["data"] == []
    for key in ([1, 1], ["a\u0000", 1], ["a\ud800", 1], ["a", 1.5]):  # subscriptions are keyed by name
        cursor = cursors.encode(key, {})
        assert problem(api.get(f"/api/v1/subscriptions?cursor={cursor}"), 400)["errors"][0]["parameter"] == "cursor", key
    assert api.get(f"/api/v1/subscriptions?cursor={cursors.encode(['a', 1], {})}").status_code == 200


def test_subscription_pages_hold_their_place(seeded, database_url):
    """The cursor holds the last name, not a row number, so subscribing meanwhile doesn't repeat or skip any."""
    api, _ = seeded

    def subscribe(name):
        with psycopg.connect(database_url) as conn:
            podcast = conn.execute("INSERT INTO podcasts (platform, external_id, name) VALUES (%s, %s, %s) RETURNING id",
                                   (PLATFORM, "UC" + name.ljust(22, "x"), name)).fetchone()[0]
            conn.execute("INSERT INTO subscriptions (podcast_id, uploads, streams) VALUES (%s, true, false)", (podcast,))

    for name in ("Delta", "bravo", "Foxtrot"):
        subscribe(name)
    first = api.get("/api/v1/subscriptions?limit=2").json()
    assert [s["name"] for s in first["data"]] == ["bravo", "Delta"]
    subscribe("Alpha")  # before the cursor
    subscribe("Echo")
    second = api.get(f"/api/v1/subscriptions?limit=2&cursor={first['next_cursor']}").json()
    assert [s["name"] for s in second["data"]] == ["Echo", "Foxtrot"] and second["next_cursor"] is None


def test_method_not_allowed_says_what_is(seeded):
    api, _ = seeded
    response = api.client.delete("/api/v1/settings")
    assert response.status_code == 405 and response.headers["content-type"] == "application/problem+json"
    assert response.headers["allow"] == "GET, PUT"  # both routes on the path, not only the first
    assert api.client.post("/api/v1/subscriptions/1").headers["allow"] == "DELETE, GET, PATCH"
    # A literal path is its own route, not an instance of the template it also fits.
    assert api.client.put("/api/v1/subscriptions/check").headers["allow"] == "POST"
    assert api.client.post("/api/v1/subscriptions/videos").headers["allow"] == "GET"
    assert api.client.delete("/api/v1/compilations").headers["allow"] == "GET, POST"


@pytest.mark.parametrize("url, parameter", [
    ("/api/v1/clips?min_similarity=5", "min_similarity"),
    ("/api/v1/clips?search=bogus", "search"),
    ("/api/v1/clips/facets?group_by=episode&min_score=x", "min_score"),
    ("/api/v1/compilations?search=bogus", "search"),
    ("/api/v1/compilations?q=", "q"),
])
def test_each_problem_is_listed_once(seeded, url, parameter):
    api, _ = seeded
    errors = problem(api.get(url), 400)["errors"]
    assert [e["parameter"] for e in errors] == [parameter]
