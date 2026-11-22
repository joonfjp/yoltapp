"""The worker's lifecycle: database restarts and lost locks, double retries and stale attempts, crashes and
clean shutdowns, which transcript a run reuses, kept audio and abandoned work folders, resumed Highlightr jobs,
and what speaker inference reads."""

import os
import time

import httpx
import psycopg
import pytest

from tests.support.fakes import VIDEO, submit, wait_for
from yolt import ingest, speakers
from yolt.db import INTERRUPTED_ERROR, MAX_INTERRUPTIONS, Database, RunCancelled, WorkerLock
from yolt.pipeline import AUDIO_KEEP_SECONDS, Worker

META = {"title": "Great Episode", "channel": "Some Pod", "duration_seconds": 20}
AUTO_JSON3 = {"events": [
    {"tStartMs": 0, "dDurationMs": 4900, "segs": [
        {"utf8": "So"}, {"utf8": " here's", "tOffsetMs": 300}, {"utf8": " the", "tOffsetMs": 700},
        {"utf8": " thing.", "tOffsetMs": 1000}]},
    {"tStartMs": 5000, "dDurationMs": 4900, "segs": [
        {"utf8": "Nobody"}, {"utf8": " saw", "tOffsetMs": 400}, {"utf8": " it", "tOffsetMs": 700},
        {"utf8": " coming.", "tOffsetMs": 900}]},
]}
AUTO_TRACKS = {"automatic_captions": {
    "en-orig": [{"ext": "json3", "url": "https://yt.test/api/timedtext?lang=en&kind=asr&fmt=json3"}]},
    "language": "en"}


@pytest.fixture
def db(database_url):
    database = Database(database_url)
    yield database
    database.close()


def terminate(database_url, pid=None):
    """What a PostgreSQL restart does to this database's connections (all but the caller's, or one)."""
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() "
                      "AND pid <> pg_backend_pid() AND (%s::integer IS NULL OR pid = %s)", (pid, pid))


def test_the_pool_replaces_connections_a_database_restart_killed(db, database_url):
    run_id = db.submit(VIDEO, "", META)
    terminate(database_url)
    assert db.run(run_id)["id"] == run_id


def test_a_worker_that_lost_its_lock_leaves_the_queue_until_it_has_it_again(db, database_url, tmp_path, monkeypatch):
    worker = Worker(db, tmp_path, idle_seconds=0)
    other = WorkerLock(database_url)
    processed = []
    monkeypatch.setattr(worker, "process", processed.append)
    try:
        assert worker._lock.acquire()
        worker.active = True
        terminate(database_url, worker._lock.conn.info.backend_pid)  # a restart drops the lock...
        assert other.acquire()  # ...and another server takes it
        run_id = db.submit(VIDEO, "", META)
        worker._tick()
        assert worker.active is False and processed == [] and db.run(run_id)["status"] == "queued"
        other.release()  # the other server stopped
        worker._tick()
        assert worker.active and processed == [run_id]
        worker._tick()  # still held: nothing new to do
        assert worker._lock.held() and processed == [run_id]
    finally:
        other.release()
        worker._lock.release()


def test_a_second_retry_doesnt_reset_a_run_the_worker_already_claimed(db, tmp_path):
    run_id = db.submit(VIDEO, "", META)
    assert db.claim_next_run() == run_id
    db.fail_run(run_id, "boom")
    worker = Worker(db, tmp_path)
    assert worker.enqueue(run_id) is True and worker._wake.is_set()  # the first click
    worker._wake.clear()
    assert db.claim_next_run() == run_id
    db.set_stage(run_id, "transcribing")
    assert worker.enqueue(run_id) is False and not worker._wake.is_set()  # the second click
    run = db.run(run_id)
    assert (run["status"], run["stage"], run["attempts"]) == ("processing", "transcribing", 2)


def test_an_attempt_cancelled_and_queued_again_cant_write_or_finish_the_run(db):
    run_id = db.submit(VIDEO, "", META)
    assert db.claim_next_run() == run_id
    attempt = db.run(run_id)["attempts"]
    words = ingest.word_rows([{"text": "Hello.", "start_seconds": 0, "end_seconds": 1}])
    clips = ingest.build_clips({"clips": [{"start_seconds": 0, "end_seconds": 1}]}, words)
    db.update_run(run_id, attempt=attempt, progress=0.5)
    assert db.cancel_run(run_id) and db.queue_run(run_id)  # cancel, then Retry, during the final step
    for write in (lambda: db.update_run(run_id, attempt=attempt, progress=0.9),
                  lambda: db.finish_run(run_id, clips, attempt=attempt)):
        with pytest.raises(RunCancelled):
            write()
    assert db.run(run_id)["status"] == "queued" and db.run(run_id)["clip_count"] == 0
    assert db.claim_next_run() == run_id  # claimed again: only the new attempt may finish it
    with pytest.raises(RunCancelled):
        db.finish_run(run_id, clips, attempt=attempt)
    db.finish_run(run_id, clips, attempt=attempt + 1)
    assert db.run(run_id)["status"] == "ready" and db.run(run_id)["clip_count"] == 1


def test_a_run_reuses_only_a_transcript_the_captions_setting_allows(client, fake):
    fake.info_extra, fake.captions = AUTO_TRACKS, AUTO_JSON3
    auto = wait_for(client, submit(client), "ready")
    assert auto["transcript_source"] == "youtube_auto" and fake.transcribe_calls == 0
    client.put("/api/v1/settings", json={"youtube_captions": "manual"})  # creator captions only: transcribe
    parakeet = wait_for(client, submit(client, instructions="funny moments"), "ready")
    assert parakeet["transcript_source"] == "parakeet" and fake.transcribe_calls == 1
    client.put("/api/v1/settings", json={"youtube_captions": "off"})  # Parakeet's own is reused
    assert wait_for(client, submit(client, instructions="sad moments"), "ready")["transcript_id"] == \
        parakeet["transcript_id"]
    client.put("/api/v1/settings", json={"youtube_captions": "any"})  # anything: the newest
    assert wait_for(client, submit(client, instructions="hot takes"), "ready")["transcript_id"] == \
        parakeet["transcript_id"]
    assert fake.transcribe_calls == 1 and len(fake.downloads) == 1


def test_audio_is_kept_for_a_retry_after_parakeet_fails(client, fake):
    worker = client.app.state.worker
    parakeet_down = [True]

    def handler(request):
        if request.url.path == "/v1/audio/transcriptions" and parakeet_down[0]:
            return httpx.Response(503, text="GPU busy")
        return fake.handler(request)

    worker.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    run_id = submit(client)
    assert "GPU busy" in wait_for(client, run_id, "failed")["error"]
    kept = worker.audio_dir / f"{VIDEO}.mp3"
    assert kept.read_bytes() == b"ID3fake" and not list((worker.data_dir / "work").glob(f"{run_id}-*"))
    parakeet_down[0] = False
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert wait_for(client, run_id, "ready")["transcript_source"] == "parakeet"
    assert fake.downloads == [VIDEO] and not kept.exists()  # not downloaded again; gone once transcribed


def test_kept_audio_goes_when_the_run_is_cancelled_or_once_stale(db, tmp_path):
    worker = Worker(db, tmp_path)
    worker.audio_dir.mkdir(parents=True)
    stale, fresh = worker.audio_dir / "aaaaaaaaaaa.mp3", worker.audio_dir / "bbbbbbbbbbb.mp3"
    stale.write_bytes(b"ID3")
    fresh.write_bytes(b"ID3")
    old = time.time() - AUDIO_KEEP_SECONDS - 60
    os.utime(stale, (old, old))
    worker._sweep()
    assert not stale.exists() and fresh.exists()

    run_id = db.submit("bbbbbbbbbbb", "", META)
    assert db.claim_next_run() == run_id
    run = db.run(run_id)
    db.cancel_run(run_id)
    with httpx.Client() as client, pytest.raises(RunCancelled):
        worker._transcribe(run, db.settings(), client, tmp_path / "work" / str(run_id))
    assert not fresh.exists()


def test_a_restart_picks_up_the_highlightr_job_it_submitted_unless_its_inputs_changed(client, fake, database_url):
    worker = client.app.state.worker
    posts, results_fail = [], [True]

    def handler(request):
        if request.url.path == "/v2/highlights/srt-jobs":
            posts.append(request)
        if request.url.path.endswith("/result") and results_fail[0]:
            return httpx.Response(500, text="result went missing")
        return fake.handler(request)

    def crash_and_restart():
        """As if the server died waiting on Highlightr: still processing, then resumed by the next start."""
        with psycopg.connect(database_url) as conn:
            conn.execute("UPDATE runs SET status = 'processing', error = NULL, finished_at = NULL WHERE id = %s",
                         (run_id,))
        worker.db.requeue_interrupted()
        worker.wake()

    worker.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    run_id = submit(client)
    wait_for(client, run_id, "failed")
    crash_and_restart()
    wait_for(client, run_id, "failed")
    assert len(posts) == 1  # the same job, asked for its result again
    client.put("/api/v1/settings", json={"max_clips": "3"})
    crash_and_restart()
    wait_for(client, run_id, "failed")
    assert len(posts) == 2  # different inputs: a new job
    client.post(f"/api/v1/runs/{run_id}/retry")
    wait_for(client, run_id, "failed")
    assert len(posts) == 3  # Retry asks afresh even with the same inputs: that job may be what failed
    results_fail[0] = False
    crash_and_restart()
    assert wait_for(client, run_id, "ready")["clip_count"] == 2 and len(posts) == 3


def test_speaker_inference_reads_only_the_opening_words_unless_it_has_them(db, tmp_path, monkeypatch):
    run_id = db.submit(VIDEO, "", META)
    episode_id = db.run(run_id)["episode_id"]
    words = ingest.word_rows([{"text": f"w{i}", "start_seconds": i * 20, "end_seconds": i * 20 + 1}
                              for i in range(30)])  # ten minutes
    version_id = db.save_transcript(episode_id, words, source="parakeet", model="m", language="en",
                                    duration_ms=600_000)
    assert db.words(version_id, before_ms=300_000) == words[:15] and db.words(version_id) == words
    db.save_settings({"llm_endpoint": "http://llm.example/v1"})
    given = []
    monkeypatch.setattr(speakers, "infer", lambda client, settings, *, words, **_: given.append(words) or
                        {"hosts": [], "guests": []})
    worker = Worker(db, tmp_path)
    in_memory = words[:3]
    monkeypatch.setattr(db, "words", lambda *a, **k: pytest.fail("read the words again"))
    assert worker._infer_speakers(episode_id, known=(version_id, in_memory)) is True
    assert given == [in_memory]
    monkeypatch.undo()
    monkeypatch.setattr(speakers, "infer", lambda client, settings, *, words, **_: given.append(words) or
                        {"hosts": [], "guests": []})
    with psycopg.connect(db.url) as conn:
        conn.execute("UPDATE episodes SET speakers_inferred_at = NULL WHERE id = %s", (episode_id,))
    assert worker._infer_speakers() is True
    assert given[-1] == words[:15]  # the first five minutes, all inference looks at


def crash_and_restart(db: Database, run_id: int) -> None:
    """The worker claims the run, the process dies mid-run, and Yolt starts again."""
    assert db.claim_next_run() == run_id
    db.requeue_interrupted()


def test_a_run_that_keeps_crashing_the_server_fails_instead_of_resuming_forever(db):
    run_id = db.submit(VIDEO, "", META)
    for _ in range(MAX_INTERRUPTIONS - 1):
        crash_and_restart(db, run_id)
        assert db.run(run_id)["status"] == "queued"
    crash_and_restart(db, run_id)
    run = db.run(run_id)
    assert run["status"] == "failed" and run["error"] == INTERRUPTED_ERROR and run["finished_at"]
    assert db.claim_next_run() is None  # not started again

    # Retry gives it a fresh count.
    assert db.queue_run(run_id)
    crash_and_restart(db, run_id)
    assert db.run(run_id)["status"] == "queued"


def test_a_clean_shutdown_puts_the_run_back_without_counting_it(db):
    run_id = db.submit(VIDEO, "", META)
    for _ in range(MAX_INTERRUPTIONS + 1):
        assert db.claim_next_run() == run_id
        attempt = db.run(run_id)["attempts"]
        assert not db.release_run(run_id, attempt - 1)  # not that attempt's any more
        assert db.release_run(run_id, attempt)
        assert db.run(run_id)["status"] == "queued"
        db.requeue_interrupted()  # the next start: nothing was left processing
    assert db.run(run_id)["status"] == "queued"


def test_stopping_while_a_run_is_still_busy_puts_it_back(db, tmp_path):
    worker = Worker(db, tmp_path)
    run_id = db.submit(VIDEO, "", META)
    assert db.claim_next_run() == run_id
    worker._current = (run_id, db.run(run_id)["attempts"])  # e.g. still waiting on Parakeet
    worker.stop(timeout=0)
    assert db.run(run_id)["status"] == "queued"


def test_retry_asks_highlightr_afresh_but_a_restart_picks_its_job_up(db):
    run_id = db.submit(VIDEO, "", META)
    db.claim_next_run()
    db.update_run(run_id, params={"style": "s", "highlightr_job_id": "j1"})
    db.requeue_interrupted()  # a restart: the job is still the one to wait for
    assert db.run(run_id)["params"]["highlightr_job_id"] == "j1"
    db.claim_next_run()
    db.fail_run(run_id, "Highlightr job j1 was still running after 6 hours.")
    assert db.queue_run(run_id)  # Retry: that job may be what hung
    assert db.run(run_id)["params"] == {"style": "s"}


def test_a_stale_attempt_can_neither_report_progress_nor_fail_the_new_one(db):
    run_id = db.submit(VIDEO, "", META)
    db.claim_next_run()
    old = db.run(run_id)["attempts"]
    assert db.release_run(run_id, old)
    db.claim_next_run()  # e.g. another server took it over
    for write in (lambda: db.set_stage(run_id, "downloading", attempt=old),
                  lambda: db.set_step(run_id, "saving", attempt=old),
                  lambda: db.ensure_processing(run_id, attempt=old)):
        with pytest.raises(RunCancelled):
            write()
    db.fail_run(run_id, "the old attempt's error", attempt=old)
    run = db.run(run_id)
    assert run["status"] == "processing" and run["error"] is None and run["stage"] is None
    db.set_stage(run_id, "downloading", attempt=old + 1)  # the current attempt still can
    assert db.run(run_id)["stage"] == "downloading"


def test_a_failure_saved_late_is_recorded_not_requeued(db, tmp_path):
    worker = Worker(db, tmp_path, idle_seconds=0)
    run_id = db.submit(VIDEO, "", META)
    db.claim_next_run()
    worker._unrecorded[run_id] = ("Parakeet at http://p timed out.", db.run(run_id)["attempts"])
    try:
        worker._tick()  # the database is back and the lock free: the failure lands before anything is requeued
        run = db.run(run_id)
        assert run["status"] == "failed" and run["error"] == "Parakeet at http://p timed out."
        assert worker._unrecorded == {}
    finally:
        worker._lock.release()


def test_the_sweep_removes_what_crashed_attempts_left(db, tmp_path):
    worker = Worker(db, tmp_path)
    stale, fresh = tmp_path / "work" / "7-1", tmp_path / "work" / "7-2"
    for folder in (stale, fresh):
        folder.mkdir(parents=True)
        (folder / "audio.part").write_bytes(b"x")
    old = time.time() - AUDIO_KEEP_SECONDS - 60
    os.utime(stale, (old, old))
    worker._sweep()
    assert not stale.exists() and fresh.exists()
