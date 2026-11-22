"""End-to-end: submit links, run the worker against fake Parakeet/Highlightr, read the results."""

import json
import time

import psycopg
from fastapi.testclient import TestClient

from tests.support.contract import ContractClient
from tests.support.fakes import VIDEO, submit, wait_for
from yolt import youtube
from yolt.repo import runs as runs_repo
from yolt.server import create_app


def slug_of(clip: dict) -> str:
    return clip["share_url"].rsplit("/c/", 1)[1]


def test_submit_to_clips(client, fake):
    response = client.post("/api/v1/runs", json={"url": f"https://youtu.be/{VIDEO}"})
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert response.headers["location"] == f"/api/v1/runs/{run_id}"
    assert response.json()["episode"]["title"] == "Great Episode"  # known at submit

    run = wait_for(client, run_id, "ready")
    assert run["episode"]["podcast"] == "Some Pod" and run["episode"]["podcast_url"] == "https://www.youtube.com/@pod"
    assert run["episode"]["published_at"] == "2026-11-01" and run["episode"]["duration_ms"] == 20000
    assert run["clip_count"] == 2 and run["finished_at"] and run["transcript_source"] == "parakeet"

    first, second = run["clips"]
    assert first["rank"] == 1 and first["title"] == "The Thing" and first["summary"] == "d1"
    assert first["text"] == "So here's the thing."
    assert first["duration_ms"] == 4850 and first["time"] == {"start_ms": 0, "end_ms": 4850}
    assert first["links"]["embed"].endswith(f"/embed/{VIDEO}?start=0&end=5&rel=0&playsinline=1")
    assert second["links"]["watch"] == f"https://www.youtube.com/watch?v={VIDEO}&t=5s"
    assert second["share_url"] == f"http://testserver/c/{slug_of(second)}"
    assert second["thumbnail_url"] == f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg"

    # Highlightr got the SRT plus the clip-length settings.
    assert 'name="min_clip_duration_seconds"\r\n\r\n20' in fake.highlightr_fields
    assert 'name="max_clip_duration_seconds"\r\n\r\n60' in fake.highlightr_fields
    assert 'name="candidates_per_window"\r\n\r\n3' in fake.highlightr_fields
    assert 'name="max_clips"\r\n\r\n8' in fake.highlightr_fields
    assert "llm_api_key" not in fake.highlightr_fields
    assert "00:00:00,000 -->" in fake.highlightr_fields

    clip = client.get(f"/api/v1/clips/{first['id']}?include=details").json()
    assert clip["episode"]["id"] == run["episode"]["id"] and clip["details"]["category"] == {"primary": "insight"}
    recent = client.get("/api/v1/clips?sort=-created&limit=12").json()["data"]
    assert [c["id"] for c in recent] == [first["id"], second["id"]]

    text = client.get(f"/api/v1/transcripts/{run['transcript_id']}/text")
    assert text.text.startswith("So here's the thing.")
    srt = client.get(f"/api/v1/transcripts/{run['transcript_id']}/srt")
    assert srt.headers["content-type"].startswith("application/x-subrip")
    assert srt.text.startswith("1\n00:00:00,000 --> ") and "So here's the thing." in srt.text

    assert client.get(f"/api/v1/shares/{slug_of(first)}").json()["id"] == first["id"]
    share = client.get(f"/c/{slug_of(first)}")
    assert share.status_code == 200
    assert '<meta property="og:title" content="The Thing">' in share.text
    assert "<title>The Thing</title>" in share.text


def test_resubmitting_makes_another_run(client, fake):
    run_id = submit(client)
    wait_for(client, run_id, "ready")
    again = client.post("/api/v1/runs", json={"url": f"https://www.youtube.com/watch?v={VIDEO}"})
    assert again.status_code == 202 and again.json()["id"] != run_id
    assert wait_for(client, again.json()["id"], "ready")["clip_count"] == 2
    assert len(client.get("/api/v1/runs").json()["data"]) == 2
    assert fake.downloads == [VIDEO] and fake.transcribe_calls == 1  # the transcript is reused


def test_failure_then_retry_reuses_transcript(client, fake):
    fake.fail_highlightr = True
    run_id = submit(client)
    failed = wait_for(client, run_id, "failed")
    assert "HTTP 503" in failed["error"] and failed["stage"] == "identifying"

    fake.fail_highlightr = False
    retried = client.post(f"/api/v1/runs/{run_id}/retry")
    assert retried.status_code == 202 and retried.json()["status"] in ("queued", "processing", "ready")
    assert wait_for(client, run_id, "ready")["attempts"] == 2
    assert fake.transcribe_calls == 1
    assert fake.downloads == [VIDEO]
    again = client.post(f"/api/v1/runs/{run_id}/retry")
    assert again.status_code == 409 and again.json()["type"].endswith("/not-failed")
    assert client.post("/api/v1/runs/999/retry").status_code == 404


def test_resubmitting_after_a_failed_run_makes_a_new_one(client, fake):
    fake.fail_highlightr = True
    run_id = submit(client)
    wait_for(client, run_id, "failed")
    fake.fail_highlightr = False
    again = client.post("/api/v1/runs", json={"url": VIDEO})
    assert again.status_code == 202 and again.json()["id"] != run_id
    wait_for(client, again.json()["id"], "ready")
    assert run_of(client, run_id)["status"] == "failed"  # Retry is what starts that one again
    assert fake.transcribe_calls == 1


OTHER = "jNQXAC9IVRw"


def run_of(client, run_id: int) -> dict:
    return client.get(f"/api/v1/runs/{run_id}").json()


def wait_until(check, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_cancel_queued_and_processing_runs_then_run_them_again(client, fake):
    fake.hold_highlightr.set()
    first = submit(client)
    wait_until(lambda: run_of(client, first)["stage"] == "identifying" and fake.job_polls)
    second = submit(client, url=OTHER)
    assert run_of(client, second)["status"] == "queued"

    cancelled = client.post(f"/api/v1/runs/{second}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["stage"] is None
    assert cancelled.json()["finished_at"]
    assert [r["id"] for r in client.get("/api/v1/runs?status=cancelled").json()["data"]] == [second]

    stopped = client.post(f"/api/v1/runs/{first}/cancel").json()
    assert stopped["status"] == "cancelled" and stopped["stage"] == "identifying"  # where it stopped
    again = client.post(f"/api/v1/runs/{first}/cancel")
    assert again.status_code == 200 and again.json() == stopped  # cancelling twice changes nothing

    # Retrying a cancelled run queues it. The worker only gets to it once it has let go of the first run,
    # which Highlightr still reports as running.
    assert client.post(f"/api/v1/runs/{second}/retry").status_code == 202
    wait_until(lambda: run_of(client, second)["stage"] == "identifying")
    fake.hold_highlightr.clear()
    assert wait_for(client, second, "ready")["clip_count"] == 2
    gone = run_of(client, first)
    assert gone["status"] == "cancelled" and gone["clip_count"] == 0

    assert client.post(f"/api/v1/runs/{first}/retry").status_code == 202
    assert wait_for(client, first, "ready")["attempts"] == 2
    assert fake.transcribe_calls == 2 and fake.downloads == [VIDEO, OTHER]  # its transcript was kept

    done = client.post(f"/api/v1/runs/{first}/cancel")
    assert done.status_code == 409 and done.json()["type"].endswith("/not-active")
    assert client.post("/api/v1/runs/999/cancel").status_code == 404


def idle_queue(client) -> bool:
    queue = client.get("/api/v1/queue").json()
    return (queue["processing"], queue["queued"]) == ([], [])


def test_the_queue_is_the_running_run_then_the_queued_ones_in_order(client, fake):
    assert idle_queue(client)
    fake.hold_highlightr.set()
    first = submit(client)
    wait_until(lambda: run_of(client, first)["stage"] == "identifying")
    second = submit(client, url=OTHER)
    third = submit(client, instructions="only the jokes")  # same video, new instructions: another run
    queue = client.get("/api/v1/queue").json()
    assert [r["id"] for r in queue["processing"]] == [first]
    assert queue["processing"][0]["stage"] == "identifying"  # a full run, as GET /runs/{id} has it
    assert [q["id"] for q in queue["queued"]] == [second, third]
    assert queue["queued"][1]["episode_id"] == run_of(client, first)["episode"]["id"]

    client.post(f"/api/v1/runs/{second}/cancel")
    assert [q["id"] for q in client.get("/api/v1/queue").json()["queued"]] == [third]
    fake.hold_highlightr.clear()
    wait_for(client, third, "ready")
    assert idle_queue(client)


def test_the_queue_carries_the_sidebar_totals(client, fake):
    assert client.get("/api/v1/queue").json()["totals"] == {"subscriptions": 0, "episodes": 0}
    client.post("/api/v1/subscriptions", json={"url": "@pod"})
    wait_for(client, submit(client), "ready")
    wait_for(client, submit(client, instructions="only the jokes"), "ready")  # the same episode, a second run
    submit(client, url=OTHER)
    assert client.get("/api/v1/queue").json()["totals"] == {"subscriptions": 1, "episodes": 2}


def test_cancelling_mid_download_stops_the_download(client, fake):
    fake.hold_downloads.add(VIDEO)  # never released: only the cancel can end this download
    first = submit(client)
    wait_until(lambda: run_of(client, first)["stage"] == "downloading")
    assert client.post(f"/api/v1/runs/{first}/cancel").json()["status"] == "cancelled"
    second = submit(client, url=OTHER)
    assert wait_for(client, second, "ready")["clip_count"] == 2
    assert fake.downloads == [VIDEO, OTHER] and fake.transcribe_calls == 1
    assert run_of(client, first)["status"] == "cancelled"
    assert not (client.app.state.worker.data_dir / "work" / str(first)).exists()


def test_embedding_failures_are_reported_until_they_recover(client, fake, database_url):
    def embeddings():
        return client.get("/api/v1/").json()["embeddings"]

    def episode():
        return client.get(f"/api/v1/episodes/{run['episode']['id']}").json()

    assert client.put("/api/v1/settings", json={"embed_url": "http://embeddings.example/v1/embeddings"}).status_code == 200
    fake.embeddings.down = True
    run = wait_for(client, submit(client), "ready")  # embedding is best effort: the run is ready anyway
    wait_until(lambda: embeddings()["last_error"])
    down = embeddings()
    assert down["pending_clips"] == 2 and "embedding server" in down["last_error"]["message"]
    assert down["last_error"]["at"]
    assert (episode()["clip_count"], episode()["embedded_clip_count"]) == (2, 0)

    # A clip with its own vector still waits for its passages' vectors.
    with psycopg.connect(database_url) as conn:
        conn.execute("INSERT INTO clip_vectors (clip_id, model_name, text_digest, embedding) "
                     "SELECT min(id), %s, 'x', %s::vector FROM clips", (down["model"], str([0.1] * 1024)))
    assert embeddings()["pending_clips"] == 2 and episode()["embedded_clip_count"] == 0

    fake.embeddings.down = False
    worker = client.app.state.worker
    worker._backfill_after = 0  # skip the retry delay
    worker.wake()
    wait_until(lambda: embeddings()["pending_clips"] == 0)
    assert embeddings()["last_error"] is None and episode()["embedded_clip_count"] == 2


def test_rejects_non_youtube_urls(client):
    response = client.post("/api/v1/runs", json={"url": "https://example.com/video"})
    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error["pointer"] == "/url" and error["code"] == "not_youtube" and "YouTube" in error["message"]


def test_unavailable_videos_are_refused_at_submit(client, fake):
    fake.info_error = youtube.YouTubeError("Private video. Sign in if you've been granted access.")
    response = client.post("/api/v1/runs", json={"url": VIDEO})
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "video_unavailable"
    assert client.get("/api/v1/runs").json()["data"] == []


def test_settings_round_trip_and_validation(client, fake):
    view = client.get("/api/v1/settings").json()
    assert view["values"]["parakeet_url"] == "http://127.0.0.1:5092"
    assert view["values"]["highlightr_url"] == "http://127.0.0.1:8000"
    assert view["values"]["llm_endpoint"] == view["values"]["embed_url"] == ""
    assert view["defaults"]["max_clips"] == view["values"]["max_clips"] == "8"
    assert view["defaults"]["candidates_per_window"] == "3"

    saved = client.put("/api/v1/settings", json={
        "parakeet_url": "http://parakeet.example:5092/", "llm_api_key": "sk-secret", "max_clips": "5",
    }).json()
    assert saved["values"]["parakeet_url"] == "http://parakeet.example:5092"
    assert saved["values"]["llm_api_key"] == ""  # never echoed back
    assert saved["secrets_configured"] == ["llm_api_key"]

    bad = client.put("/api/v1/settings", json={"highlightr_url": "ftp://x", "max_clips": "-1"})
    assert bad.status_code == 422
    assert {e["pointer"] for e in bad.json()["errors"]} == {"/highlightr_url", "/max_clips"}
    too_long = client.put("/api/v1/settings", json={"min_clip_seconds": "90"})
    assert too_long.status_code == 422 and too_long.json()["errors"][0]["pointer"] == "/min_clip_seconds"
    unknown = client.put("/api/v1/settings", json={"colour": "blue"})
    assert unknown.status_code == 422 and unknown.json()["errors"][0]["code"] == "unknown_field"

    cleared = client.put("/api/v1/settings", json={"llm_api_key": ""}).json()
    assert cleared["secrets_configured"] == []

    # An empty value is the default again, for every setting.
    blank = {k: "" for k in ("parakeet_url", "parakeet_model", "highlightr_url", "llm_endpoint", "embed_url",
                             "embed_model", "min_clip_seconds", "max_clip_seconds", "max_clips",
                             "candidates_per_window", "clip_instructions")}
    reset = client.put("/api/v1/settings", json=blank)
    assert reset.status_code == 200, reset.text
    assert reset.json()["values"] == view["values"]
    assert client.put("/api/v1/settings", json={"min_clip_seconds": "70", "max_clip_seconds": ""}).status_code == 422

    ok = client.post("/api/v1/settings/test", json={"service": "parakeet", "url": "http://p:5092"}).json()
    assert ok == {"ok": True, "message": "Parakeet is reachable."}
    assert client.post("/api/v1/settings/test", json={"service": "gpu", "url": "x"}).status_code == 422


def test_llm_test_button(client, fake):
    def test(**body):
        return client.post("/api/v1/settings/test", json={"service": "llm", **body}).json()

    # A full /chat/completions URL is used as-is.
    url = "http://llm.example/v1/chat/completions"
    assert test(url=url) == {"ok": True, "message": "LLM replied (model zion)."}
    sent = fake.llm_requests[-1]
    assert str(sent.url) == url and "authorization" not in sent.headers
    assert "model" not in json.loads(sent.content)

    # A versioned base URL gets /chat/completions appended; saved key and model are used.
    client.put("/api/v1/settings", json={"llm_endpoint": "http://llm:7777/v1", "llm_api_key": "sk-saved",
                                         "llm_model": "m1"})
    assert test(url="http://llm:7777/v1/")["ok"]
    sent = fake.llm_requests[-1]
    assert str(sent.url) == "http://llm:7777/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer sk-saved"
    assert json.loads(sent.content)["model"] == "m1"
    assert test(url="http://llm:7777/v1/chat/completions")["ok"]  # the same endpoint, spelled out
    assert fake.llm_requests[-1].headers["authorization"] == "Bearer sk-saved"

    # The saved key never leaves for any other URL: GET /settings hides it, so Test mustn't hand it over.
    assert test(url="http://elsewhere.test:7777/v1")["ok"]
    assert "authorization" not in fake.llm_requests[-1].headers

    # Form values win over saved ones; an empty key sends none.
    test(url=url, model="m2", api_key="sk-typed")
    assert fake.llm_requests[-1].headers["authorization"] == "Bearer sk-typed"
    assert json.loads(fake.llm_requests[-1].content)["model"] == "m2"
    test(url=url, api_key="")
    assert "authorization" not in fake.llm_requests[-1].headers

    failed = test(url="http://down/v1")
    assert not failed["ok"] and "HTTP 401" in failed["message"]
    blank = test(url="")
    assert not blank["ok"] and "Highlightr will use" in blank["message"]


def test_api_key_is_sent_to_highlightr(client, fake):
    client.put("/api/v1/settings", json={"llm_api_key": "sk-secret", "llm_model": "m1"})
    wait_for(client, submit(client), "ready")
    assert 'name="llm_api_key"\r\n\r\nsk-secret' in fake.highlightr_fields
    assert 'name="llm_model"\r\n\r\nm1' in fake.highlightr_fields


def test_env_override_locks_setting(client, monkeypatch):
    monkeypatch.setenv("YOLT_HIGHLIGHTR_URL", "http://gpu-box:8000")
    view = client.get("/api/v1/settings").json()
    assert view["values"]["highlightr_url"] == "http://gpu-box:8000"
    assert view["env_overrides"] == ["highlightr_url"]
    locked = client.put("/api/v1/settings", json={"highlightr_url": "http://x:1"})
    assert locked.status_code == 409
    assert locked.json()["errors"] == [{"code": "locked", "pointer": "/highlightr_url",
                                        "message": "`highlightr_url` is set by YOLT_HIGHLIGHTR_URL and can't be changed here."}]


def test_cross_origin_writes_are_blocked(client):
    response = client.post("/api/v1/runs", json={"url": VIDEO}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and response.headers["content-type"] == "application/problem+json"
    response = client.put("/api/v1/settings", json={"parakeet_url": "http://evil:1"},
                          headers={"Origin": "http://testserver"})
    assert response.status_code == 200


def test_non_json_bodies_are_refused(client):
    # A cross-site HTML form can only send form/text bodies; those must not be accepted.
    response = client.post("/api/v1/runs", content=f'{{"url": "{VIDEO}"}}', headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    response = client.post("/api/v1/runs", content="{nope", headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["title"] == "The body is not valid JSON"


def test_foreign_host_header_rejected(fake, tmp_path, database_url):
    app = create_app(tmp_path, database_url=database_url, start_worker=False)  # default: loopback hosts only
    with TestClient(app, base_url="http://attacker.example") as test_client:
        assert test_client.get("/api/v1/runs").status_code == 400
    with TestClient(app, base_url="http://127.0.0.1:8484") as test_client:
        assert ContractClient(test_client).get("/api/v1/runs").status_code == 200


def test_no_other_site_can_frame_yolt(client, fake, tmp_path, database_url, monkeypatch):
    """Every response forbids framing (clickjacking Subscribe or Retry) and sniffing, and nothing else: the UI
    embeds YouTube players, which need a referrer, and the dev live reload is an inline script."""
    responses = [client.get(path) for path in ("/", "/c/missing", "/static/app.js", "/api/docs", "/api/openapi.json",
                                               "/nope", "/api/v1/runs", "/api/v1/runs/999")]
    responses.append(client.post("/api/v1/runs", json={"url": VIDEO}, headers={"Origin": "https://evil.example"}))
    app = create_app(tmp_path, database_url=database_url, start_worker=False)
    with TestClient(app, base_url="http://attacker.example") as other:
        responses.append(other.get("/"))  # refused by the host check
    monkeypatch.setattr(runs_repo, "list_runs", lambda *a, **k: 1 / 0)
    with TestClient(app, base_url="http://127.0.0.1:8484", raise_server_exceptions=False) as crashing:
        responses.append(crashing.get("/api/v1/runs"))
    assert [r.status_code for r in responses] == [200, 200, 200, 200, 200, 404, 200, 404, 403, 400, 500]
    for response in responses:
        assert response.headers["content-security-policy"] == "frame-ancestors 'none'", response.url
        assert response.headers["x-frame-options"] == "DENY", response.url
        assert response.headers["x-content-type-options"] == "nosniff", response.url
        assert "referrer-policy" not in response.headers


def test_ui_routes_serve_shell(client):
    for path in ("/", "/settings", "/r/1", "/c/missing", "/search?q=x", "/themes", "/saved", "/subscriptions",
                 "/episodes", "/tv", "/tv?usage=never", "/compilations", "/labs/thread"):
        response = client.get(path)
        assert response.status_code == 200
        assert '<main class="panel main"' in response.text
    assert client.get("/draft").status_code == 404  # the draft page is gone from the UI
    for asset in ("app.js", "tv.js", "tv.css", "vendor/qrcode.mjs"):
        assert client.get(f"/static/{asset}").status_code == 200, asset
    assert client.get("/api/openapi.json").json()["info"]["title"] == "Yolt API"
    docs = client.get("/api/docs")
    assert docs.status_code == 200 and "http://testserver/api/v1" in docs.text and "createRun" in docs.text


# --- YouTube captions instead of Parakeet -------------------------------------

AUTO_JSON3 = {"events": [
    {"tStartMs": 0, "dDurationMs": 4900, "segs": [
        {"utf8": "So"}, {"utf8": " here's", "tOffsetMs": 300}, {"utf8": " the", "tOffsetMs": 700},
        {"utf8": " thing.", "tOffsetMs": 1000}]},
    {"tStartMs": 4000, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 5000, "dDurationMs": 4900, "segs": [
        {"utf8": "Nobody"}, {"utf8": " saw", "tOffsetMs": 400}, {"utf8": " it", "tOffsetMs": 700},
        {"utf8": " coming.", "tOffsetMs": 900}]},
]}
AUTO_TRACKS = {"automatic_captions": {
    "en-orig": [{"ext": "json3", "url": "https://yt.test/api/timedtext?lang=en&kind=asr&fmt=json3"}],
    "fr": [{"ext": "json3", "url": "https://yt.test/api/timedtext?lang=en&tlang=fr&fmt=json3"}],
}, "language": "en"}


def test_manual_skips_auto_captions(client, fake):
    client.put("/api/v1/settings", json={"youtube_captions": "manual"})
    fake.info_extra = AUTO_TRACKS  # auto-captions only
    fake.captions = AUTO_JSON3
    run = wait_for(client, submit(client), "ready")
    assert run["transcript_source"] == "parakeet" and fake.transcribe_calls == 1
    assert client.get("/api/v1/lookup", params={"url": VIDEO}).json()["transcript_source"] == "parakeet"


def test_youtube_captions_skip_download_and_parakeet(client, fake):
    fake.info_extra = AUTO_TRACKS
    fake.captions = AUTO_JSON3
    run = wait_for(client, submit(client), "ready")

    assert fake.downloads == [] and fake.transcribe_calls == 0
    assert fake.fetched_track.source == "youtube_auto" and "tlang" not in fake.fetched_track.url
    assert run["transcript_source"] == "youtube_auto"
    # Same downstream shape as a Parakeet transcript.
    assert run["clips"][0]["text"] == "So here's the thing."
    transcript = client.get(f"/api/v1/transcripts/{run['transcript_id']}").json()
    assert transcript["source"] == "youtube_auto" and transcript["language"] == "en" and transcript["model"] is None
    srt = client.get(f"/api/v1/transcripts/{run['transcript_id']}/srt").text
    assert "1\n00:00:00,000 --> 00:00:02,000\nSo here's the thing.\n\n2\n" in srt
    assert "\nNobody saw it coming.\n" in srt
    assert "00:00:00,000 --> 00:00:02,000\nSo here's the thing." in fake.highlightr_fields


def test_unusable_captions_fall_back_to_parakeet(client, fake):
    fake.info_extra = AUTO_TRACKS
    fake.captions = youtube.YouTubeError("HTTP 429")
    run = wait_for(client, submit(client), "ready")
    assert fake.transcribe_calls == 1
    assert run["transcript_source"] == "parakeet"


def test_partial_captions_fall_back_to_parakeet(client, fake):
    fake.info_extra = {**AUTO_TRACKS, "duration": 3600}  # captions stop at 10s of an hour
    fake.captions = AUTO_JSON3
    assert wait_for(client, submit(client), "ready")["transcript_source"] == "parakeet"


def test_captions_setting_off_always_transcribes(client, fake):
    assert client.put("/api/v1/settings", json={"youtube_captions": "off"}).status_code == 200
    assert client.put("/api/v1/settings", json={"youtube_captions": "sometimes"}).status_code == 422
    fake.info_extra = AUTO_TRACKS
    fake.captions = AUTO_JSON3
    assert wait_for(client, submit(client), "ready")["transcript_source"] == "parakeet"
    assert fake.transcribe_calls == 1


# --- checking a link before submitting ------------------------------------------


def test_lookup_reports_captions_before_submitting(client, fake):
    fake.info_extra = AUTO_TRACKS
    body = client.get("/api/v1/lookup", params={"url": f"https://youtu.be/{VIDEO}"}).json()
    assert body["title"] == "Great Episode" and body["channel"] == "Some Pod" and body["duration_ms"] == 20000
    assert body["captions"] == {"creator": None, "auto": "en"}
    assert body["transcript_source"] == "youtube_auto"
    assert body["runs"] == []
    assert body["thumbnail_url"] == f"https://i.ytimg.com/vi/{VIDEO}/mqdefault.jpg"

    client.put("/api/v1/settings", json={"youtube_captions": "off"})
    body = client.get("/api/v1/lookup", params={"url": VIDEO}).json()
    assert body["captions"]["auto"] == "en" and body["transcript_source"] == "parakeet"


def test_lookup_without_captions(client, fake):
    body = client.get("/api/v1/lookup", params={"url": VIDEO}).json()
    assert body["captions"] == {"creator": None, "auto": None}
    assert body["transcript_source"] == "parakeet"


def test_lookup_then_submit_fetches_video_info_once(client, fake):
    fake.info_extra = AUTO_TRACKS
    fake.captions = AUTO_JSON3
    client.get("/api/v1/lookup", params={"url": VIDEO})
    run_id = submit(client)
    wait_for(client, run_id, "ready")
    assert fake.info_calls == 1
    [existing] = client.get("/api/v1/lookup", params={"url": VIDEO}).json()["runs"]
    assert existing["id"] == run_id and existing["transcript_source"] == "youtube_auto"


def test_lookup_errors(client, fake):
    response = client.get("/api/v1/lookup", params={"url": "https://vimeo.com/1"})
    assert response.status_code == 422 and response.json()["errors"][0]["parameter"] == "url"
    fake.info_error = youtube.YouTubeError("Private video. Sign in if you've been granted access.")
    response = client.get("/api/v1/lookup", params={"url": VIDEO})
    assert response.status_code == 422
    assert response.json()["errors"][0]["message"].startswith("Private video")
    assert client.get("/api/v1/lookup").status_code == 400


def test_asset_urls_change_with_their_content(client):
    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    version = page.text.split("/static/app.js?v=")[1].split('"')[0]
    assert len(version) == 10 and version != "0.1.0"


def test_live_reload_is_dev_only(client, fake, tmp_path, database_url):
    assert client.get("/api/dev/version").status_code == 404
    assert "/api/dev/version" not in client.get("/").text

    app = create_app(tmp_path / "dev", database_url=database_url, start_worker=False,
                     allowed_hosts=["testserver"], dev=True)
    with TestClient(app) as dev_client:
        assert "/api/dev/version" in dev_client.get("/").text
        first = dev_client.get("/api/dev/version").text
        assert first == dev_client.get("/api/dev/version").text


# --- clip instructions ------------------------------------------------------------

HOOK = "A strong hook in the first 3 seconds and high retention; no setup needed."


def test_instructions_are_sent_to_highlightr_as_style(client, fake):
    run = wait_for(client, submit(client, instructions=f"  {HOOK} "), "ready")
    assert run["instructions"] == HOOK
    assert f'name="style"\r\n\r\n{HOOK}' in fake.highlightr_fields


def test_no_instructions_uses_the_settings_default(client, fake):
    default = "insightful, surprising, emotionally engaging, with a strong hook in the first few seconds"
    first = wait_for(client, submit(client), "ready")
    assert first["instructions"] == default
    assert f'name="style"\r\n\r\n{default}' in fake.highlightr_fields

    client.put("/api/v1/settings", json={"clip_instructions": "Funny moments only"})
    second = client.post("/api/v1/runs", json={"url": "jNQXAC9IVRw", "instructions": ""}).json()
    assert second["instructions"] == "Funny moments only"
    wait_for(client, second["id"], "ready")
    assert 'name="style"\r\n\r\nFunny moments only' in fake.highlightr_fields
    assert client.put("/api/v1/settings", json={"clip_instructions": ""}).json()["values"]["clip_instructions"] == default


def test_every_submission_reclips_the_video_reusing_its_transcript(client, fake):
    first = submit(client)
    old_clips = wait_for(client, first, "ready")["clips"]

    again = client.post("/api/v1/runs", json={"url": VIDEO})  # the same link and focus: another run
    assert again.status_code == 202 and again.json()["id"] != first
    assert again.headers["location"] == f"/api/v1/runs/{again.json()['id']}"
    wait_for(client, again.json()["id"], "ready")

    hook = client.post("/api/v1/runs", json={"url": VIDEO, "instructions": HOOK})
    assert hook.status_code == 202 and hook.json()["id"] not in (first, again.json()["id"])
    run = wait_for(client, hook.json()["id"], "ready")
    assert fake.downloads == [VIDEO] and fake.transcribe_calls == 1  # transcript reused
    assert run["transcript_source"] == "parakeet"

    # The first run's share links still work, and the episode counts every run's clips.
    assert client.get(f"/api/v1/shares/{slug_of(old_clips[0])}").status_code == 200
    assert len(client.get("/api/v1/runs").json()["data"]) == 3
    episode = client.get(f"/api/v1/episodes/{run['episode']['id']}").json()
    assert episode["run_count"] == 3 and episode["clip_count"] == 6 and len(episode["transcripts"]) == 1


def test_instructions_length_is_limited(client):
    response = client.post("/api/v1/runs", json={"url": VIDEO, "instructions": "x" * 1001})
    assert response.status_code == 422 and response.json()["errors"][0]["pointer"] == "/instructions"
    assert client.put("/api/v1/settings", json={"clip_instructions": "x" * 1001}).status_code == 422


def test_run_options_override_settings_for_that_run(client, fake):
    first = submit(client)
    wait_for(client, first, "ready")
    assert client.get(f"/api/v1/runs/{first}").json()["options"] == {}

    options = {"min_clip_seconds": 30, "max_clip_seconds": 90, "max_clips": 5, "candidates_per_window": 7}
    response = client.post("/api/v1/runs", json={"url": VIDEO, "options": options})
    assert response.status_code == 202 and response.json()["id"] != first  # new options: another run
    assert response.json()["options"] == options
    run = wait_for(client, response.json()["id"], "ready")
    for field, value in (("min_clip_duration_seconds", 30), ("max_clip_duration_seconds", 90), ("max_clips", 5),
                         ("candidates_per_window", 7)):
        assert f'name="{field}"\r\n\r\n{value}' in fake.highlightr_fields
    assert fake.transcribe_calls == 1  # the transcript is reused

    # No options (or {}) follows Settings.
    plain = wait_for(client, submit(client, options={}), "ready")
    assert plain["options"] == {} and 'name="max_clips"\r\n\r\n8' in fake.highlightr_fields

    # One option given: the rest follow Settings, which the run doesn't change.
    fewer = wait_for(client, submit(client, options={"max_clips": 3}), "ready")
    assert fewer["options"] == {"max_clips": 3}
    assert 'name="max_clips"\r\n\r\n3' in fake.highlightr_fields
    assert 'name="min_clip_duration_seconds"\r\n\r\n20' in fake.highlightr_fields
    assert client.get("/api/v1/settings").json()["values"]["max_clips"] == "8"


def test_run_options_are_validated(client):
    def pointers(options) -> list[str]:
        response = client.post("/api/v1/runs", json={"url": VIDEO, "options": options})
        assert response.status_code == 422, response.text
        return [error["pointer"] for error in response.json()["errors"]]

    assert pointers({"candidates_per_window": 11}) == ["/options/candidates_per_window"]
    assert pointers({"max_clips": 0}) == ["/options/max_clips"]
    assert pointers({"max_clips": True}) == ["/options/max_clips"]
    assert pointers({"min_clip_seconds": "30"}) == ["/options/min_clip_seconds"]  # a number, not text
    assert pointers({"max_length": 10}) == ["/options/max_length"]
    assert pointers({"min_clip_seconds": 40, "max_clip_seconds": 30}) == ["/options/min_clip_seconds"]
    assert pointers({"min_clip_seconds": 61}) == ["/options/min_clip_seconds"]  # longer than Settings' 60s maximum
    assert pointers({"max_clip_seconds": 10}) == ["/options/max_clip_seconds"]  # shorter than Settings' 20s minimum
    assert client.get("/api/v1/runs").json()["data"] == []


# Real auto-captions: per-word timing, no punctuation, no capitals.
BARE_JSON3 = {"events": [
    {"tStartMs": 0, "dDurationMs": 4900, "segs": [
        {"utf8": "so"}, {"utf8": " here's", "tOffsetMs": 300}, {"utf8": " the", "tOffsetMs": 700},
        {"utf8": " thing", "tOffsetMs": 1000}]},
    {"tStartMs": 5000, "dDurationMs": 4900, "segs": [
        {"utf8": "nobody"}, {"utf8": " saw", "tOffsetMs": 400}, {"utf8": " it", "tOffsetMs": 700},
        {"utf8": " coming", "tOffsetMs": 900}]},
]}


def test_unpunctuated_transcripts_skip_the_cut_point_checks(client, fake):
    fake.info_extra = AUTO_TRACKS
    fake.captions = BARE_JSON3
    run = wait_for(client, submit(client), "ready")
    assert client.get(f"/api/v1/transcripts/{run['transcript_id']}").json()["punctuated"] is False
    first, second = run["clips"]
    context = client.get(f"/api/v1/clips/{second['id']}/context").json()
    assert context["starts_mid_sentence"] is None and context["follows_question"] is None
    review = client.post("/api/v1/segments/review", json={"segments": [{"clip_id": first["id"]},
                                                                       {"clip_id": second["id"]}]}).json()
    codes = [f["code"] for f in review["findings"]]
    assert "cut_points_unchecked" in codes
    assert not {"starts_mid_sentence", "ends_mid_sentence", "follows_question"} & set(codes)
    assert review["items"][1]["starts_mid_sentence"] is None

    # A punctuated transcript (Parakeet here) is checked as before.
    fake.info_extra = {}  # no captions for the next video
    other = wait_for(client, submit(client, url="jNQXAC9IVRw"), "ready")
    assert client.get(f"/api/v1/transcripts/{other['transcript_id']}").json()["punctuated"] is True
    punct = client.post("/api/v1/segments/review", json={"segments": [{"clip_id": other["clips"][1]["id"]}]}).json()
    assert "cut_points_unchecked" not in [f["code"] for f in punct["findings"]]
    assert punct["items"][0]["starts_mid_sentence"] is False


def test_candidates_per_window_is_a_setting(client, fake):
    assert client.get("/api/v1/settings").json()["values"]["candidates_per_window"] == "3"
    for bad in ("0", "11", "three", "-1"):
        response = client.put("/api/v1/settings", json={"candidates_per_window": bad})
        assert response.status_code == 422 and response.json()["errors"][0]["pointer"] == "/candidates_per_window"
    assert client.put("/api/v1/settings", json={"candidates_per_window": "8"}).status_code == 200
    wait_for(client, submit(client), "ready")
    assert 'name="candidates_per_window"\r\n\r\n8' in fake.highlightr_fields
