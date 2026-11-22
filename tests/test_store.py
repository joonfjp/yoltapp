"""What a run leaves in PostgreSQL, the run queue, and clip immutability."""

import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from tests.support.contract import ContractClient
from tests.support.fakes import SPOKEN, VIDEO, submit, wait_for
from tests.support.seed import seed
from yolt import ingest
from yolt.db import MIGRATIONS_DIR, Database, DatabaseUnavailable, RunCancelled, migrate
from yolt.server import create_app

HOOK = "A strong hook in the first 3 seconds."


def rows(database_url: str, sql: str, *params) -> list[dict]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def test_a_run_stores_words_clips_quotes_and_passages(client, fake, database_url):
    client.put("/api/v1/settings", json={"llm_api_key": "sk-secret"})
    run_id = submit(client)
    wait_for(client, run_id, "ready")

    [tv] = rows(database_url, "SELECT * FROM transcript_versions")
    words = rows(database_url, "SELECT seq, word, start_ms, end_ms FROM transcript_words ORDER BY seq")
    assert tv["source"] == "parakeet" and tv["language"] == "en" and tv["duration_ms"] == 20000
    assert [w["seq"] for w in words] == list(range(1, 15)) and tv["word_count"] == 14
    assert tv["full_text"] == SPOKEN == " ".join(w["word"] for w in words)

    first, second = rows(database_url, "SELECT * FROM clips ORDER BY rank")
    assert (first["title"], first["summary"], first["local_clip_id"]) == ("The Thing", "d1", "clip_001")
    assert (first["start_ms"], first["end_ms"], first["first_word_seq"], first["last_word_seq"]) == (0, 4850, 1, 4)
    assert (second["title"], second["first_word_seq"], second["last_word_seq"]) == ("Second", 5, 9)
    assert first["details"]["category"] == {"primary": "insight"}

    [quote] = rows(database_url, "SELECT * FROM clip_quotes")
    assert (quote["clip_id"], quote["quote_text"], quote["first_word_seq"], quote["last_word_seq"]) == (
        first["id"], "here's the thing", 2, 4)
    passages = rows(database_url, "SELECT * FROM passages ORDER BY clip_id")
    assert [(p["clip_id"], p["first_word_seq"], p["last_word_seq"], p["text"]) for p in passages] == [
        (first["id"], 1, 4, "So here's the thing."),
        (second["id"], 5, 9, "Nobody saw it coming, honestly."),
    ]

    [run] = rows(database_url, "SELECT * FROM runs")
    assert run["transcript_version_id"] == tv["id"] and run["attempts"] == 1 and run["finished_at"]
    assert run["params"]["min_clip_duration_seconds"] == "20" and "llm_api_key" not in run["params"]
    [episode] = rows(database_url, "SELECT e.*, p.name, p.url FROM episodes e JOIN podcasts p ON p.id = e.podcast_id")
    assert (episode["video_id"], episode["title"], episode["duration_ms"]) == (VIDEO, "Great Episode", 20000)
    assert (episode["name"], episode["url"]) == ("Some Pod", "https://www.youtube.com/@pod")


def test_reclipping_reuses_the_transcript_and_keeps_earlier_clips(client, fake, database_url):
    wait_for(client, submit(client), "ready")
    before = rows(database_url, "SELECT id, slug, run_id, first_word_seq FROM clips ORDER BY id")
    wait_for(client, submit(client, instructions=HOOK), "ready")

    after = rows(database_url, "SELECT id, slug, run_id, first_word_seq FROM clips ORDER BY id")
    assert after[:2] == before and len(after) == 4
    assert len(rows(database_url, "SELECT id FROM transcript_versions")) == 1
    assert len({r["transcript_version_id"] for r in rows(database_url, "SELECT * FROM runs")}) == 1
    assert fake.transcribe_calls == 1


META = {"title": "Great Episode", "channel": "Some Pod", "duration_seconds": 20}


def make_ready_run(db: Database) -> dict:
    run = db.run(db.submit(VIDEO, "", META))
    words = ingest.word_rows([{"text": "Hello", "start_seconds": 0, "end_seconds": 0.5},
                              {"text": "there.", "start_seconds": 0.6, "end_seconds": 1.0}])
    version = db.save_transcript(run["episode_id"], words, source="youtube", model=None, language="en",
                                 duration_ms=None)
    assert db.save_transcript(run["episode_id"], words, source="youtube", model=None, language="en",
                              duration_ms=None) == version  # same words: same version
    db.update_run(run["id"], transcript_version_id=version)
    db.finish_run(run["id"], ingest.build_clips({"clips": [{"start_seconds": 0, "end_seconds": 1}]}, words))
    return db.run(run["id"])


def test_a_cancelled_run_is_never_resumed_finished_or_failed(database_url):
    db = Database(database_url)
    try:
        run_id = db.submit(VIDEO, "", META)
        assert db.claim_next_run() == run_id
        db.set_stage(run_id, "fetching")
        assert db.cancel_run(run_id) and not db.cancel_run(run_id)
        words = ingest.word_rows([{"text": "Hello.", "start_seconds": 0, "end_seconds": 1}])
        clips = ingest.build_clips({"clips": [{"start_seconds": 0, "end_seconds": 1}]}, words)
        for write in (lambda: db.set_stage(run_id, "identifying"), lambda: db.set_step(run_id, "saving"),
                      lambda: db.update_run(run_id, progress=0.5), lambda: db.ensure_processing(run_id),
                      lambda: db.finish_run(run_id, clips)):
            with pytest.raises(RunCancelled):
                write()
        db.fail_run(run_id, "boom")  # e.g. a service error landing just after the cancel
        run = db.run(run_id)
        assert run["status"] == "cancelled" and run["stage"] == "fetching"
        assert run["error"] is None and run["clip_count"] == 0
        assert db.requeue_interrupted() == 0 and db.claim_next_run() is None
    finally:
        db.close()


def test_clips_are_never_replaced(database_url):
    db = Database(database_url)
    try:
        run = make_ready_run(db)
        assert run["status"] == "ready" and run["clip_count"] == 1
        with pytest.raises(RuntimeError, match="never replaced"):
            db.finish_run(run["id"], [])
        assert db.run(run["id"])["clip_count"] == 1
    finally:
        db.close()


def test_queue_claims_oldest_first_and_requeues_interrupted_runs(database_url):
    db = Database(database_url)
    try:
        a = db.submit("aaaaaaaaaaa", "", META)
        b = db.submit("bbbbbbbbbbb", "", META)
        assert [db.claim_next_run(), db.claim_next_run(), db.claim_next_run()] == [a, b, None]
        assert db.run(a)["status"] == "processing"
        assert db.requeue_interrupted() == 2
        assert db.claim_next_run() == a
    finally:
        db.close()


def test_concurrent_submissions_of_the_same_link_make_one_episode_and_a_run_each(database_url):
    db = Database(database_url)
    barrier = threading.Barrier(6)

    def go(_):
        barrier.wait()
        return db.submit(VIDEO, HOOK, META)

    try:
        with ThreadPoolExecutor(6) as pool:
            run_ids = list(pool.map(go, range(6)))
        assert len(set(run_ids)) == 6
        assert len({db.run(run_id)["episode_id"] for run_id in run_ids}) == 1
    finally:
        db.close()


def test_restart_resumes_an_interrupted_run(fake, tmp_path, database_url):
    db = Database(database_url)
    run_id = db.submit(VIDEO, "", META)
    assert db.claim_next_run() == run_id  # the server died mid-run
    db.close()

    app = create_app(tmp_path, database_url=database_url, allowed_hosts=["testserver"], poll_seconds=0,
                     client_factory=lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    with TestClient(app) as client:
        assert wait_for(ContractClient(client), run_id, "ready")["clip_count"] == 2


def test_migrations_are_idempotent(database_url):
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        assert migrate(conn) == []
    db = Database(database_url)
    try:
        assert db.schema_version() == len(list(MIGRATIONS_DIR.glob("*.sql")))
    finally:
        db.close()


def test_the_search_vector_migration_fills_in_stored_transcripts(database_url):
    """0011 stores each transcript's tsvector for keyword search, computing it for transcripts already there."""
    db = Database(database_url)
    try:
        seed(db, "aaaaaaaaaaa")
    finally:
        db.close()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:  # back to before 0011, the transcript kept
        conn.execute("ALTER TABLE transcript_versions DROP COLUMN fts")  # and its index
        conn.execute("CREATE INDEX transcript_versions_fts_idx ON transcript_versions "
                     "USING gin (to_tsvector('english', full_text))")
        conn.execute("DELETE FROM schema_migrations WHERE version = 11")
        conn.commit()
        assert migrate(conn) == ["0011_transcript_search_vector"]
        assert conn.execute("SELECT fts = to_tsvector('english', full_text) AS same, "
                            "fts @@ websearch_to_tsquery('english', 'consciousness') AS found "
                            "FROM transcript_versions").fetchall() == [{"same": True, "found": True}]
        assert conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'transcript_versions_fts_idx'"
                            ).fetchone()["indexdef"].endswith("USING gin (fts)")


def test_unreachable_database_error_hides_the_password():
    with pytest.raises(DatabaseUnavailable) as error:
        Database("postgresql://yolt:hunter2@127.0.0.1:1/yolt", wait_seconds=0.5)
    assert "hunter2" not in str(error.value) and "yolt:***@127.0.0.1:1/yolt" in str(error.value)


def vector_counts(database_url: str) -> tuple[int, int]:
    [row] = rows(database_url, "SELECT (SELECT count(*) FROM clip_vectors) AS c, (SELECT count(*) FROM passage_vectors) AS p")
    return row["c"], row["p"]


def test_a_ready_run_is_embedded(client, fake, database_url):
    assert client.put("/api/v1/settings", json={"embed_url": "http://embeddings.example/v1/embeddings"}).status_code == 200
    wait_for(client, submit(client), "ready")
    deadline = time.time() + 5
    while vector_counts(database_url) != (2, 2) and time.time() < deadline:
        time.sleep(0.05)
    assert vector_counts(database_url) == (2, 2)
    [model] = {r["model_name"] for r in rows(database_url, "SELECT model_name FROM clip_vectors")}
    assert model == "Qwen/Qwen3-Embedding-8B@1024"
    texts = [t for batch in fake.embeddings.calls for t in batch]
    assert "The Thing\nd1\nhere's the thing" in texts and "So here's the thing." in texts


def test_an_embedding_outage_never_fails_a_run_and_is_caught_up(client, fake, database_url):
    assert client.put("/api/v1/settings", json={"embed_url": "http://embeddings.example/v1/embeddings"}).status_code == 200
    fake.embeddings.down = True
    run = wait_for(client, submit(client), "ready")
    assert run["clip_count"] == 2 and vector_counts(database_url) == (0, 0)

    fake.embeddings.down = False
    worker = client.app.state.worker
    worker._backfill_after = 0  # skip the retry delay
    worker.wake()
    deadline = time.time() + 5
    while vector_counts(database_url) != (2, 2) and time.time() < deadline:
        time.sleep(0.05)
    assert vector_counts(database_url) == (2, 2)


def test_fixing_the_embedding_settings_catches_up_without_waiting(client, fake, database_url):
    assert client.put("/api/v1/settings", json={"embed_url": "http://embeddings.example/v1/embeddings"}).status_code == 200
    fake.embeddings.down = True
    wait_for(client, submit(client), "ready")
    assert client.app.state.worker._backfill_after > time.monotonic()  # backing off after the failure
    fake.embeddings.down = False
    client.put("/api/v1/settings", json={"embed_url": "http://127.0.0.1:8098/v1/embeddings"})
    deadline = time.time() + 5
    while vector_counts(database_url) != (2, 2) and time.time() < deadline:
        time.sleep(0.05)
    assert vector_counts(database_url) == (2, 2)


def test_the_punctuation_backfill_matches_the_python_rule(database_url):
    """Migration 0006 classifies existing transcripts with SQL; it must agree with ingest.punctuated."""
    sql = (pathlib.Path(__file__).resolve().parent.parent / "yolt" / "migrations" / "0006_punctuated.sql").read_text()
    pattern = sql.split("w.word ~ '", 1)[1].split("')", 1)[0].replace("''", "'")
    words = ["end.", 'end."', "end?)", "end!”", "end’.", "mid", "e.g", "Mr", "done.]"]
    with psycopg.connect(database_url) as conn:
        for word in words:
            matched = conn.execute("SELECT %s ~ %s", (word, pattern)).fetchone()[0]
            assert matched == bool(ingest.SENTENCE_END.search(word)), word
