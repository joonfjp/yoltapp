"""The feed (the TV page): GET /feed picks clips to play next, POST /feed/plays counts plays, and feed_mix."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.support.contract import ContractClient
from tests.support.embedder import FakeEmbeddingServer
from tests.support.seed import TALKS, seed
from yolt import embed
from yolt.db import Database
from yolt.embeddings import EmbeddingService
from yolt.server import create_app

B = "/api/v1"


@pytest.fixture
def server():
    return FakeEmbeddingServer()


@pytest.fixture
def catalogue(database_url, server):
    """Two ready runs of four clips each, embedded."""
    db = Database(database_url)
    try:
        db.save_settings({"embed_url": "http://embeddings.example/v1/embeddings"})
        runs = {video: seed(db, video) for video in TALKS}
        embed.embed_pending(db, EmbeddingService(db.settings, server.client_factory()))
        return runs
    finally:
        db.close()


@pytest.fixture
def api(catalogue, tmp_path, database_url, server):
    app = create_app(tmp_path, database_url=database_url, start_worker=False, allowed_hosts=["testserver"],
                     client_factory=server.client_factory())
    with TestClient(app) as client:
        yield ContractClient(client)


def feed(api, query="") -> dict:
    response = api.get(f"{B}/feed{query}")
    assert response.status_code == 200, response.text
    return response.json()


def problem(response, status) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def test_a_batch_has_every_clip_once_alternating_episodes(api):
    body = feed(api)
    assert body["candidates"] == 8 and len(body["data"]) == 8
    assert body["mix"] == {"last_3_days": 50, "last_7_days": 30, "last_30_days": 10, "last_90_days": 5, "all_time": 5}
    clips = [item["clip"] for item in body["data"]]
    assert len({c["id"] for c in clips}) == 8
    episodes = [c["episode"]["id"] for c in clips]
    assert all(a != b for a, b in zip(episodes, episodes[1:]))  # four clips each: they can always alternate
    item = body["data"][0]
    assert item["plays"] == 0 and item["tier"] in ("last_3_days", "all_time")  # every clip was just made
    assert "quotes" in item["clip"] and "speakers" in item["clip"]


def test_each_pick_carries_its_timed_words(api):
    for item in feed(api, "?count=3")["data"]:
        clip, words = item["clip"], item["words"]
        assert " ".join(w["word"] for w in words) == clip["text"]
        assert [w["seq"] for w in words] == list(range(clip["words"]["first"], clip["words"]["last"] + 1))
        assert words[0]["start_ms"] == clip["time"]["start_ms"] and words[-1]["end_ms"] == clip["time"]["end_ms"]


def test_recent_clips_wait_and_the_last_ones_episode_is_avoided(api, catalogue):
    first = feed(api, f"?run_id={catalogue['aaaaaaaaaaa']['run_id']}")["data"]
    recent = [item["clip"]["id"] for item in first[:3]]
    for _ in range(5):
        picks = feed(api, "?count=4&" + "&".join(f"recent={i}" for i in recent))["data"]
        assert not {item["clip"]["id"] for item in picks[:4]} & set(recent)
        assert picks[0]["clip"]["episode"]["id"] == catalogue["bbbbbbbbbbb"]["episode_id"]


def test_a_filtered_feed_repeats_rather_than_stopping(api, catalogue):
    run = catalogue["aaaaaaaaaaa"]["run_id"]
    everything = [item["clip"]["id"] for item in feed(api, f"?run_id={run}")["data"]]
    assert len(everything) == 4
    again = feed(api, f"?run_id={run}&count=6&" + "&".join(f"recent={i}" for i in everything))
    assert again["candidates"] == 4 and sorted(i["clip"]["id"] for i in again["data"]) == sorted(everything)


def test_filters_are_the_same_as_clips(api, catalogue):
    body = feed(api, "?q=jobs")
    assert body["candidates"] == 3
    assert all("job" in item["clip"]["text"].lower() for item in body["data"])
    assert feed(api, "?usage=picked")["data"] == []
    meaning = feed(api, "?q=consciousness&search=semantic&min_similarity=0.3")
    assert meaning["data"] and all(item["clip"]["episode"]["id"] == catalogue["aaaaaaaaaaa"]["episode_id"]
                                   for item in meaning["data"])


def test_bad_parameters_are_400s(api):
    semantic = problem(api.get(f"{B}/feed?q=minds&search=hybrid"), 400)
    assert semantic["errors"][0]["parameter"] == "min_similarity"
    assert problem(api.get(f"{B}/feed?count=25"), 400)["errors"][0]["parameter"] == "count"
    assert problem(api.get(f"{B}/feed?" + "&".join(f"recent={i}" for i in range(51))), 400)
    assert problem(api.get(f"{B}/feed?limit=3"), 400)["errors"][0]["code"] == "unknown_parameter"
    assert problem(api.get(f"{B}/feed?theme=k9-9-0000"), 422)["errors"][0]["code"] == "theme_not_found"


def test_plays_are_counted_and_reported(api):
    clip_id = feed(api, "?count=1")["data"][0]["clip"]["id"]
    first = api.post(f"{B}/feed/plays", json={"clip_id": clip_id})
    assert first.status_code == 200 and first.json()["plays"] == 1
    assert api.post(f"{B}/feed/plays", json={"clip_id": clip_id}).json()["plays"] == 2
    plays = {item["clip"]["id"]: item["plays"] for item in feed(api)["data"]}
    assert plays[clip_id] == 2 and sum(plays.values()) == 2


def test_a_play_of_no_such_clip_is_refused(api):
    assert problem(api.post(f"{B}/feed/plays", json={"clip_id": 999999}), 422)["errors"][0]["code"] == "clip_not_found"
    for body in ({}, {"clip_id": "1"}, {"clip_id": 0}, {"clip_id": 1, "extra": True}, {"clip_id": 2 ** 63}):
        assert problem(api.post(f"{B}/feed/plays", json=body), 422)["errors"]
    assert problem(api.post(f"{B}/feed/plays", content="clip_id=1",
                            headers={"Content-Type": "application/x-www-form-urlencoded"}), 415)
    assert problem(api.post(f"{B}/feed/plays", json={"clip_id": 1}, headers={"Origin": "https://evil.example"}), 403)


def test_the_mix_picks_by_clip_age(api, catalogue, database_url):
    with psycopg.connect(database_url) as conn:  # the second run's clips were made 200 days ago
        conn.execute("UPDATE clips SET created_at = now() - interval '200 days' WHERE run_id = %s",
                     (catalogue["bbbbbbbbbbb"]["run_id"],))
    old = catalogue["bbbbbbbbbbb"]["episode_id"]
    assert api.put(f"{B}/settings", json={"feed_mix": "100,0,0,0,0"}).status_code == 200
    body = feed(api)
    assert body["mix"]["last_3_days"] == 100
    for item in body["data"]:  # alternating episodes forces the old clips in, through "all time"
        assert item["tier"] == ("all_time" if item["clip"]["episode"]["id"] == old else "last_3_days")
    # Clips older than every tier still play, through "all time", even when every weight that fits is 0.
    assert api.put(f"{B}/settings", json={"feed_mix": "0,0,0,100,0"}).status_code == 200
    assert {i["tier"] for i in feed(api, f"?episode_id={old}")["data"]} == {"all_time"}


def test_the_mix_setting_is_validated(api):
    bad = problem(api.put(f"{B}/settings", json={"feed_mix": "50,50"}), 422)
    assert bad["errors"][0]["pointer"] == "/feed_mix"
    assert problem(api.put(f"{B}/settings", json={"feed_mix": "0,0,0,0,0"}), 422)
    saved = api.put(f"{B}/settings", json={"feed_mix": " 10, 0,0,0, 90 "}).json()
    assert saved["values"]["feed_mix"] == "10, 0,0,0, 90"
    assert feed(api)["mix"]["all_time"] == 90
    assert api.put(f"{B}/settings", json={"feed_mix": ""}).json()["values"]["feed_mix"] == "50,30,10,5,5"
