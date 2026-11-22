"""The failure paths found in review: the worker survives database errors, one worker per database,
speaker retries rotate, hung services give up, failed runs leave no files, and the scripts don't clobber."""

import importlib.util
import json
import time
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.support.contract import ContractClient
from tests.support.embedder import FakeEmbeddingServer
from tests.support.fakes import VIDEO, submit, wait_for
from tests.support.seed import TALKS, seed
from yolt import embed, services, youtube
from yolt.db import Database
from yolt.embeddings import EmbeddingService
from yolt.pipeline import Worker
from yolt.server import create_app


def test_a_database_error_recording_a_failure_doesnt_kill_the_worker(client, fake):
    worker = client.app.state.worker
    real = worker.db.fail_run
    calls = []

    def flaky(run_id, error, **kw):
        calls.append(run_id)
        if len(calls) == 1:
            raise psycopg.OperationalError("the database went away")
        real(run_id, error, **kw)

    worker.db.fail_run = flaky
    fake.fail_highlightr = True
    run_id = submit(client)
    assert wait_for(client, run_id, "failed")["error"]  # recorded on the next tick, once it worked
    assert calls == [run_id, run_id] and worker.thread.is_alive()
    fake.fail_highlightr = False
    assert wait_for(client, submit(client, url="jNQXAC9IVRw"), "ready")["clip_count"] == 2


def test_one_worker_per_database(client, fake, tmp_path, database_url):
    assert client.app.state.worker.active
    second = create_app(tmp_path / "second", database_url=database_url, allowed_hosts=["testserver"],
                        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)), poll_seconds=0)
    with TestClient(second) as other:
        assert second.state.worker.active is False and not second.state.worker.thread.is_alive()
        run_id = ContractClient(other).post("/api/v1/runs", json={"url": VIDEO}).json()["id"]
        assert wait_for(client, run_id, "ready")  # the first server's worker did it


def test_shutdown_mid_run_leaves_it_to_resume(database_url, tmp_path, monkeypatch):
    db = Database(database_url)
    try:
        run_id = db.submit(VIDEO, "", {"title": "T"})
        worker = Worker(db, tmp_path)

        def interrupted(claimed):
            worker._current = (claimed, db.run(claimed)["attempts"])  # what process() records first
            raise services.Interrupted

        monkeypatch.setattr(worker, "process", interrupted)
        worker._tick()
        run = db.run(run_id)
        assert run["status"] == "queued" and not run["error"]  # back in the queue, not failed
        assert db.requeue_interrupted() == 0  # nothing left processing for the next start to count
    finally:
        db.close()


def test_highlightr_waits_have_a_deadline_and_stop():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "j", "status": "queued"})
        return httpx.Response(200, json={"job_id": "j", "status": "running"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(services.ServiceError, match="still running"):
            services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0, deadline_seconds=0)
        with pytest.raises(services.Interrupted):
            services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0, should_stop=lambda: True)


def highlightr(polls: list, *, known: set[str] = frozenset({"j"})):
    """A Highlightr whose job polls answer `polls` in turn: a status dict, an httpx.Response, or an
    exception class to raise. It knows the jobs in `known` and numbers new ones from j2."""
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(202, json={"job_id": f"j{len(posts) + 1}", "status": "queued"})
        job = request.url.path.split("/")[4]
        if job not in known and job != f"j{len(posts) + 1}":
            return httpx.Response(404, json={"detail": "Job not found"})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"clips": [], "job": job})
        answer = polls.pop(0) if polls else {"status": "completed"}
        if isinstance(answer, type):
            raise answer("Highlightr went away", request=request)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json={"job_id": job, **answer})

    return httpx.Client(transport=httpx.MockTransport(handler)), posts


def test_highlightr_polling_rides_out_a_brief_outage(monkeypatch):
    monkeypatch.setattr(services, "RETRY_SECONDS", 0)
    client, posts = highlightr([httpx.ConnectError, httpx.Response(502), httpx.ReadTimeout, httpx.Response(503),
                                {"status": "running"}, httpx.Response(504), {"status": "completed"}])
    with client:
        result = services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0)
    assert result["clips"] == [] and len(posts) == 1


def test_a_sustained_highlightr_outage_or_a_real_error_still_fails_the_wait(monkeypatch):
    monkeypatch.setattr(services, "RETRY_SECONDS", 0)
    client, _ = highlightr([httpx.ConnectError] * 3)
    with client, pytest.raises(services.ServiceError, match="didn't answer for 0 minutes"):
        services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0, outage_seconds=0)
    client, _ = highlightr([httpx.ConnectError] * 3)
    with client, pytest.raises(services.Interrupted):  # stopping isn't held up by the outage
        services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0,
                                     should_stop=iter([False, True]).__next__)
    client, _ = highlightr([httpx.Response(500, text="boom")])
    with client, pytest.raises(services.ServiceError, match="HTTP 500"):
        services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0)


def test_a_job_highlightr_forgets_mid_wait_fails_with_a_clear_error():
    polls = [httpx.Response(200, json={"job_id": "j2", "status": "running"}),
             httpx.Response(404, json={"detail": "Job not found"})]

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "j2", "status": "queued"})
        return polls.pop(0)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, \
            pytest.raises(services.ServiceError, match="no longer has job j2"):
        services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0)


def test_an_earlier_highlightr_job_is_picked_up_again_if_highlightr_still_has_it():
    jobs = []
    client, posts = highlightr([{"status": "running"}, {"status": "completed"}])
    with client:  # still running: polled until done, nothing submitted
        result = services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0, job_id="j",
                                              on_job=jobs.append)
    assert result["job"] == "j" and posts == [] and jobs == ["j"]

    client, posts = highlightr([], known=set())
    with client:  # Highlightr restarted (or the job expired): 404, so a new one is submitted
        result = services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0, job_id="j",
                                              on_job=jobs.append)
    assert len(posts) == 1 and result["job"] == "j2" and jobs[-1] == "j2"

    client, posts = highlightr([{"status": "failed", "error": "LLM down"}])
    with client:  # the job that failed isn't the answer to a retry
        result = services.identify_highlights(client, "http://h", "1\n", {}, poll_seconds=0, job_id="j")
    assert len(posts) == 1 and result["job"] == "j2"


def test_thumbnails_are_removed_when_a_run_cant_be_stored(client, fake, monkeypatch):
    def frame(self, seconds, out):
        out.write_bytes(b"\xff\xd8 jpeg")
        return True

    monkeypatch.setattr(youtube.StoryboardFrames, "frame", frame)
    worker = client.app.state.worker

    def broken(run_id, clips, **_):
        assert all(worker.thumbs_dir.joinpath(f"{c.slug}.jpg").exists() for c in clips)
        raise psycopg.OperationalError("disk full")

    monkeypatch.setattr(worker.db, "finish_run", broken)
    wait_for(client, submit(client), "failed")
    assert list(worker.thumbs_dir.iterdir()) == []


# --- speakers: one bad episode can't block the rest ------------------------------------------


def llm(answers: dict[str, object]):
    """An LLM that answers by the video title in the prompt: a dict is JSON, an int an HTTP status."""
    def handler(request):
        prompt = json.loads(request.content)["messages"][1]["content"]
        for title, answer in answers.items():
            if title in prompt:
                if isinstance(answer, int):
                    return httpx.Response(answer)
                content = answer if isinstance(answer, str) else json.dumps(answer)
                return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        raise AssertionError(prompt)
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def episode_state(database_url, video):
    with psycopg.connect(database_url) as conn:
        return conn.execute(
            "SELECT speakers_attempts, speakers_inferred_at IS NOT NULL FROM episodes WHERE video_id = %s",
            (video,)).fetchone()


def test_a_failing_episode_doesnt_block_the_others_and_is_given_up_on(database_url, tmp_path):
    db = Database(database_url)
    try:
        for video in TALKS:
            seed(db, video)
        db.save_settings({"llm_endpoint": "http://llm.example/v1"})
        worker = Worker(db, tmp_path, client_factory=llm({
            "Minds and Machines": "not json at all", "Jobs After AI": {"hosts": ["Jo"], "guests": []}}))
        assert worker._infer_speakers() is True  # Minds (oldest) fails, but the LLM works: go on
        assert episode_state(database_url, "aaaaaaaaaaa") == (1, False)
        assert worker._infer_speakers() is True  # Jobs, tried next instead of Minds again
        assert episode_state(database_url, "bbbbbbbbbbb") == (0, True)
        worker._infer_speakers()
        worker._infer_speakers()
        assert episode_state(database_url, "aaaaaaaaaaa") == (3, False)
        assert db.speakers_pending() is None  # given up on; a person can still set its speakers
    finally:
        db.close()


def test_an_llm_outage_isnt_counted_against_the_episode(database_url, tmp_path):
    db = Database(database_url)
    try:
        seed(db, "aaaaaaaaaaa")
        db.save_settings({"llm_endpoint": "http://llm.example/v1"})
        worker = Worker(db, tmp_path, client_factory=llm({"Minds and Machines": 503}))
        for _ in range(5):
            worker._infer_speakers()
        assert episode_state(database_url, "aaaaaaaaaaa") == (0, False)
        assert worker._speakers_after > time.monotonic()  # backing off instead
        assert db.speakers_pending() is not None
    finally:
        db.close()


# --- scripts and labels -------------------------------------------------------------------------


def test_embeddings_are_labeled_with_the_model_that_made_them(database_url):
    db = Database(database_url)
    try:
        seed(db, "aaaaaaaaaaa")
        reads = []

        def settings():  # the model changes right after the first read
            reads.append(1)
            return {"embed_url": "http://e/v1/embeddings", "embed_model": "model-a" if len(reads) == 1 else "model-b"}

        embed.embed_pending(db, EmbeddingService(settings, FakeEmbeddingServer().client_factory()))
        with psycopg.connect(database_url) as conn:
            models = {r[0] for r in conn.execute("SELECT model_name FROM clip_vectors UNION SELECT model_name FROM passage_vectors")}
        assert models == {"model-a@1024"}
    finally:
        db.close()


def load_rotate():
    path = Path(__file__).resolve().parent.parent / "dev" / "rotate_db_password.py"
    spec = importlib.util.spec_from_file_location("rotate_db_password", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rotating_the_password_changes_only_the_database_url(database_url, tmp_path):
    rotate = load_rotate()
    env, password_file = tmp_path / ".env", tmp_path / ".db-password"
    env.write_text(f"# mine\nYOLT_DB_PORT=5489\nYOLT_DATABASE_URL={database_url}\nYOLT_LLM_API_KEY=sk-keep\n")
    rotate.rotate(env, password_file)
    lines = env.read_text().splitlines()
    password = password_file.read_text()
    assert lines[0] == "# mine" and lines[1] == "YOLT_DB_PORT=5489" and lines[3] == "YOLT_LLM_API_KEY=sk-keep"
    assert lines[2] == "YOLT_DATABASE_URL=" + rotate.with_password(database_url, password)
    assert f"postgres:{password}@" in lines[2] and len(password) == 48
    assert oct(env.stat().st_mode)[-3:] == "600" and oct(password_file.stat().st_mode)[-3:] == "600"
    assert rotate.with_password("postgresql://u:old@h:1/d", "new") == "postgresql://u:new@h:1/d"


def test_highlightr_steps_and_job_id_are_reported(client, fake, database_url):
    statuses = iter([{"status": "running", "stage": "identifying", "progress": {"windows_done": 2, "windows_total": 2}},
                     {"status": "running", "stage": "global_ranking", "progress": {"windows_done": 2, "windows_total": 2}}])
    seen = []

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "job-7", "status": "queued", "stage": "queued", "progress": {}})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"clips": []})
        status = next(statuses, {"status": "completed", "stage": "done", "progress": {}})
        return httpx.Response(200, json={"job_id": "job-7", **status})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        services.identify_highlights(http, "http://h", "1\n", {}, poll_seconds=0,
                                     on_progress=lambda d, t, s: seen.append((d, t, s)), on_job=seen.append)
    assert seen[0] == "job-7" and (2, 2, "global_ranking") in seen

    run = wait_for(client, submit(client), "ready")
    with psycopg.connect(database_url) as conn:
        params = conn.execute("SELECT params FROM runs WHERE id = %s", (run["id"],)).fetchone()[0]
    assert params["highlightr_job_id"] == "j1" and "llm_api_key" not in params


def test_the_run_reports_each_highlightr_step(client, fake):
    real = fake.handler
    script = iter(["queued", "identifying", "global_ranking", "global_ranking", "enriching_metadata"])

    def stepwise(request):
        if request.url.path == "/v2/highlights/jobs/j1":
            stage = next(script, None)
            if stage:
                done = 1 if stage == "identifying" else (0 if stage == "queued" else 2)
                return httpx.Response(200, json={"job_id": "j1", "status": "running", "stage": stage,
                                                 "progress": {"windows_done": done, "windows_total": 2}})
        return real(request)

    worker = client.app.state.worker
    worker.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(stepwise))
    worker.poll_seconds = 0.3
    run_id = submit(client)
    seen, deadline = [], time.time() + 15
    while time.time() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["step"] and (not seen or seen[-1][0] != run["step"]):
            seen.append((run["step"], run["detail"], run["progress"], bool(run["step_started_at"])))
        if run["status"] == "ready":
            break
        time.sleep(0.05)
    steps = [s[0] for s in seen]
    assert steps[:4] == ["waiting", "discovering", "ranking", "writing_headlines"], seen
    discovering = next(s for s in seen if s[0] == "discovering")
    assert discovering[1] == "1/2 windows" and discovering[2] == 0.5
    ranking = next(s for s in seen if s[0] == "ranking")
    assert ranking[2] is None and ranking[3]  # no progress inside ranking, just its clock
    assert run["status"] == "ready" and run["step"] is None
