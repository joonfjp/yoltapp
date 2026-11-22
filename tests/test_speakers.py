"""Speakers: inference after a run, outages, corrections, and speakers across the API."""

import json
import time

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.support.contract import ContractClient
from tests.support.fakes import submit, wait_for
from tests.support.seed import TALKS, seed
from yolt import ingest, speakers
from yolt.db import Database
from yolt.server import create_app

# --- the inference itself ---------------------------------------------------------------

WORDS = ingest.word_rows([{"text": t, "start_seconds": i * 100.0, "end_seconds": i * 100.0 + 0.5}
                          for i, t in enumerate(["Welcome.", "I'm", "Ada.", "Later", "words."])])
SETTINGS = {"llm_endpoint": "http://llm:7777/v1", "llm_model": "m1", "llm_api_key": "sk-1"}


def answer(content):
    return replying({"choices": [{"message": content}]})


def replying(body):
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))


def test_excerpt_is_the_first_five_minutes():
    assert speakers.excerpt(WORDS) == "0:00 Welcome.\n1:40 I'm\n3:20 Ada."  # "Later" starts at 5:00


def test_infer_sends_a_strict_schema_and_cleans_names():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"hosts": ["  Ada   Host ", "Ada Host"], "guests": ["ada host", "Gus Guest", 7]})}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        people = speakers.infer(client, SETTINGS, title="Ep 1", channel="Pod", description="With Gus", words=WORDS)
    assert people == {"hosts": ["Ada Host"], "guests": ["Gus Guest"]}
    [request] = sent
    body = json.loads(request.content)
    assert str(request.url) == "http://llm:7777/v1/chat/completions" and request.headers["authorization"] == "Bearer sk-1"
    assert body["model"] == "m1" and body["temperature"] == 0
    assert body["response_format"]["json_schema"]["strict"] is True
    text = body["messages"][1]["content"]
    assert "Ep 1" in text and "Pod" in text and "With Gus" in text and "0:00 Welcome." in text


@pytest.mark.parametrize("message", [
    {"content": "```json\n{\"hosts\": [\"A\"], \"guests\": []}\n```"},
    {"parsed": {"hosts": ["A"], "guests": []}},
    {"content": "{'hosts': ['A'], 'guests': []}"},  # a Python literal, read without evaluating anything
    {"content": [{"type": "text", "text": "{\"hosts\": [\"A\"], "}, {"type": "text", "text": "\"guests\": []}"}]},
    {"content": {"type": "text", "text": "{\"hosts\": [\"A\"], \"guests\": []}"}},  # one part, not a list
])
def test_unwraps_what_providers_return(message):
    with answer(message) as client:
        assert speakers.infer(client, SETTINGS, title=None, channel=None, description=None, words=WORDS)["hosts"] == ["A"]


@pytest.mark.parametrize("message", [
    {"refusal": "no"}, {"content": "not json"}, {"content": "[1, 2]"},
    {"content": json.dumps({"people": ["A"]})},  # misnamed keys aren't "nobody"
    {"content": json.dumps({"hosts": ["A"]})},
    {"content": json.dumps({"hosts": "A", "guests": []})},
    {"content": [{"type": "refusal", "refusal": "no"}]},
    {"content": [{"type": "image_url", "image_url": {"url": "x"}}]},
])
def test_unusable_answers_are_unavailable(message):
    with answer(message) as client, pytest.raises(speakers.SpeakersUnavailable) as raised:
        speakers.infer(client, SETTINGS, title=None, channel=None, description=None, words=WORDS)
    assert not raised.value.transient  # the LLM answered: the attempt counts


@pytest.mark.parametrize("body", [{"choices": []}, {"choices": None}, {"id": "x", "object": "chat.completion"}])
def test_a_completion_without_an_answer_is_unavailable(body):
    with replying(body) as client, pytest.raises(speakers.SpeakersUnavailable):
        speakers.infer(client, SETTINGS, title=None, channel=None, description=None, words=WORDS)


def test_no_endpoint_or_a_failing_one_is_unavailable():
    with answer({}) as client, pytest.raises(speakers.SpeakersUnavailable, match="No LLM endpoint"):
        speakers.infer(client, {**SETTINGS, "llm_endpoint": ""}, title=None, channel=None, description=None, words=WORDS)
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with down, pytest.raises(speakers.SpeakersUnavailable):
        speakers.infer(down, SETTINGS, title=None, channel=None, description=None, words=WORDS)


# --- in the worker ------------------------------------------------------------------------------


def episode_of(client, run_id: int) -> dict:
    episode_id = client.get(f"/api/v1/runs/{run_id}").json()["episode"]["id"]
    return client.get(f"/api/v1/episodes/{episode_id}").json()


def wait_for_speakers(client, run_id: int, timeout: float = 5) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        episode = episode_of(client, run_id)
        if episode["speakers"]:
            return episode
        time.sleep(0.05)
    raise AssertionError("no speakers")


def test_speakers_are_inferred_once_per_episode(client, fake):
    assert client.put("/api/v1/settings", json={"llm_endpoint": SETTINGS["llm_endpoint"]}).status_code == 200
    fake.info_extra = {"description": "Ada talks to Gus about the thing."}
    episode = wait_for_speakers(client, wait_for(client, submit(client), "ready")["id"])
    assert [(s["name"], s["role"], s["verified"], s["attribution_method"]) for s in episode["speakers"]] == [
        ("Ada Host", "host", False, "llm_metadata_inference"), ("Gus Guest", "guest", False, "llm_metadata_inference")]
    [body] = fake.speaker_requests
    prompt = body["messages"][1]["content"]
    assert "Great Episode" in prompt and "Some Pod" in prompt and "Ada talks to Gus" in prompt
    assert "0:00 So here's the thing." in prompt

    wait_for(client, submit(client, instructions="Funny bits"), "ready")  # same episode: no new inference
    time.sleep(0.2)
    assert len(fake.speaker_requests) == 1


def test_an_llm_outage_never_fails_a_run_and_is_caught_up(client, fake):
    assert client.put("/api/v1/settings", json={"llm_endpoint": SETTINGS["llm_endpoint"]}).status_code == 200
    fake.speakers_down = True
    run = wait_for(client, submit(client), "ready")
    assert run["clip_count"] == 2 and episode_of(client, run["id"])["speakers"] == []
    fake.speakers_down = False
    worker = client.app.state.worker
    worker._speakers_after = 0  # skip the retry delay
    worker.wake()
    assert [s["name"] for s in wait_for_speakers(client, run["id"])["speakers"]] == ["Ada Host", "Gus Guest"]


def test_no_llm_endpoint_means_no_inference(client, fake, monkeypatch):
    monkeypatch.delenv("YOLT_LLM_ENDPOINT", raising=False)
    run = wait_for(client, submit(client), "ready")
    time.sleep(0.2)
    assert fake.speaker_requests == [] and episode_of(client, run["id"])["speakers"] == []


def test_corrections_are_verified_and_never_overwritten(client, fake):
    assert client.put("/api/v1/settings", json={"llm_endpoint": SETTINGS["llm_endpoint"]}).status_code == 200
    run = wait_for(client, submit(client), "ready")
    episode = wait_for_speakers(client, run["id"])
    fixed = client.put(f"/api/v1/episodes/{episode['id']}/speakers", json={"speakers": [
        {"name": "Ada  Lovelace", "role": "host"}, {"name": "Gus Guest", "role": "guest"}]})
    assert fixed.status_code == 200
    assert [(s["name"], s["verified"], s["attribution_method"]) for s in fixed.json()["speakers"]] == [
        ("Ada Lovelace", True, "manual"), ("Gus Guest", True, "manual")]
    worker = client.app.state.worker
    assert worker._infer_speakers(episode["id"]) is False  # nothing pending: corrections stick
    assert client.get(f"/api/v1/episodes/{episode['id']}").json()["speakers"] == fixed.json()["speakers"]

    dupe = client.put(f"/api/v1/episodes/{episode['id']}/speakers", json={"speakers": [
        {"name": "Ada", "role": "host"}, {"name": "ada", "role": "guest"}]})
    assert dupe.status_code == 422 and dupe.json()["errors"][0]["pointer"] == "/speakers/1/name"
    assert client.put(f"/api/v1/episodes/{episode['id']}/speakers",
                      json={"speakers": [{"name": "X", "role": "cohost"}]}).status_code == 422
    assert client.put("/api/v1/episodes/999/speakers", json={"speakers": []}).status_code == 404
    nobody = client.put(f"/api/v1/episodes/{episode['id']}/speakers", json={"speakers": []})
    assert nobody.json()["speakers"] == []


# --- across the API ------------------------------------------------------------------------------


@pytest.fixture
def seeded(tmp_path, database_url):
    """Two seeded episodes sharing a confirmed guest; no worker."""
    db = Database(database_url)
    try:
        ids = {video: seed(db, video) for video in TALKS}
        db.set_speakers(ids["aaaaaaaaaaa"]["episode_id"], [("Mia Host", "host"), ("Sam Guest", "guest")])
        db.set_speakers(ids["bbbbbbbbbbb"]["episode_id"], [("Sam Guest", "guest")])
    finally:
        db.close()
    app = create_app(tmp_path, database_url=database_url, start_worker=False, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        yield ContractClient(client), ids


def test_speakers_list_and_filters(seeded):
    api, ids = seeded
    listed = api.get("/api/v1/speakers").json()["data"]
    assert [(s["name"], s["episode_count"], s["verified_count"], s["clip_count"]) for s in listed] == [
        ("Sam Guest", 2, 2, 8), ("Mia Host", 1, 1, 4)]
    assert [s["name"] for s in api.get("/api/v1/speakers?q=mia").json()["data"]] == ["Mia Host"]
    assert api.get("/api/v1/speakers?q=%25").json()["data"] == []  # % is a character, not a wildcard
    first = api.get("/api/v1/speakers?limit=1").json()
    assert [s["name"] for s in api.get(f"/api/v1/speakers?limit=1&cursor={first['next_cursor']}").json()["data"]] == ["Mia Host"]

    mia, sam = listed[1]["id"], listed[0]["id"]
    assert [e["title"] for e in api.get(f"/api/v1/episodes?speaker_id={mia}").json()["data"]] == ["Minds and Machines"]
    assert len(api.get(f"/api/v1/episodes?speaker_id={sam}").json()["data"]) == 2
    assert {c["episode"]["title"] for c in api.get(f"/api/v1/clips?speaker_id={mia}").json()["data"]} == {"Minds and Machines"}
    facets = api.get("/api/v1/clips/facets?group_by=speaker").json()
    assert facets["clip_count"] == 8
    assert [(g["label"], g["clip_count"]) for g in facets["groups"]] == [("Sam Guest", 8), ("Mia Host", 4)]


def test_include_speakers_and_the_speaker_heavy_check(seeded):
    api, ids = seeded
    clips = {c["id"]: c for c in api.get("/api/v1/clips?limit=50&include=speakers").json()["data"]}
    some = next(c for c in clips.values() if c["episode"]["title"] == "Minds and Machines")
    assert [(s["name"], s["role"]) for s in some["speakers"]] == [("Mia Host", "host"), ("Sam Guest", "guest")]
    a = [c["id"] for c in clips.values() if c["episode"]["title"] == "Minds and Machines"][:2]
    b = [c["id"] for c in clips.values() if c["episode"]["title"] == "Jobs After AI"][:1]
    rev = api.post("/api/v1/compilations", json={"title": "Sam", "items": [{"clip_id": i} for i in a + b]}).json()
    items = api.get(f"/api/v1/revisions/{rev['id']}?include=speakers").json()["items"]
    assert [s["name"] for s in items[2]["speakers"]] == ["Sam Guest"]
    review = api.get(f"/api/v1/revisions/{rev['id']}/review").json()
    heavy = [f for f in review["findings"] if f["code"] == "speaker_heavy"]
    assert heavy and heavy[0]["positions"] == [1, 2, 3] and "Sam Guest" in heavy[0]["message"]


def test_speakers_command(database_url, monkeypatch, capsys):
    db = Database(database_url)
    try:
        seed(db, "aaaaaaaaaaa")
    finally:
        db.close()
    monkeypatch.setenv("YOLT_DATABASE_URL", database_url)
    monkeypatch.setenv("YOLT_LLM_ENDPOINT", SETTINGS["llm_endpoint"])
    content = {"content": json.dumps({"hosts": ["Mia Host"], "guests": []})}
    assert speakers.main(client_factory=lambda: answer(content)) == 0
    out = capsys.readouterr().out
    assert "Minds and Machines: Mia Host" in out and "Done: speakers inferred for 1 episodes." in out
    assert speakers.main(client_factory=lambda: answer(content)) == 0
    assert "for 0 episodes" in capsys.readouterr().out


def test_an_unusable_answer_is_a_failed_attempt_not_nobody(database_url, monkeypatch, capsys):
    db = Database(database_url)
    try:
        episode_id = seed(db, "aaaaaaaaaaa")["episode_id"]
    finally:
        db.close()
    monkeypatch.setenv("YOLT_DATABASE_URL", database_url)
    monkeypatch.setenv("YOLT_LLM_ENDPOINT", SETTINGS["llm_endpoint"])

    def episode():
        with psycopg.connect(database_url) as conn:
            return conn.execute("SELECT speakers_inferred_at, speakers_attempts FROM episodes WHERE id = %s",
                                (episode_id,)).fetchone()

    misnamed = {"content": [{"type": "text", "text": json.dumps({"speakers": ["Mia Host"]})}]}
    assert speakers.main(client_factory=lambda: answer(misnamed)) == 1
    assert "skipped" in capsys.readouterr().out
    assert episode() == (None, 1)  # not saved as "nobody identified"; it's tried again
    content = {"content": [{"type": "text", "text": json.dumps({"hosts": ["Mia Host"], "guests": []})}]}
    assert speakers.main(client_factory=lambda: answer(content)) == 0
    assert "Minds and Machines: Mia Host" in capsys.readouterr().out and episode()[0] is not None
