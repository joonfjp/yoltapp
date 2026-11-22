"""PostgreSQL storage: settings, the run queue, transcripts, and clips.

The schema lives in yolt/migrations (numbered SQL files, applied in order at
startup). Reads for the HTTP API join through runs -> episodes.
"""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from .ingest import PASSAGE_VERSION, NewClip, Word, full_text, punctuated, timing_digest

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_LOCK = 0x594F4C54  # "YOLT": one migrator at a time
WORKER_LOCK = 0x594F4C57  # "YOLW": one worker per database
MAX_INTERRUPTIONS = 3  # unexpected stops while processing a run (since its last Retry) before it fails instead
INTERRUPTED_ERROR = (f"Yolt stopped unexpectedly {MAX_INTERRUPTIONS} times while working on this run "
                     "(a crash, or the machine running out of memory?), so it wasn't started again. Retry it to "
                     "try once more.")
PLATFORM = "youtube"
SETTINGS_TTL = 2.0  # seconds a read of the settings table is reused; saves here clear it at once
MAX_SPEAKER_ATTEMPTS = 3  # unusable answers for one episode before it's left for a person to fill in


class DatabaseUnavailable(RuntimeError):
    pass


class RunCancelled(Exception):
    """The run the worker is on was cancelled (or is no longer the one it claimed): stop working on it."""


@dataclass(frozen=True)
class Setting:
    default: str
    env: str | None = None
    secret: bool = False


SETTINGS: dict[str, Setting] = {
    "parakeet_url": Setting("http://127.0.0.1:5092", "YOLT_PARAKEET_URL"),
    "parakeet_model": Setting("parakeet-tdt-0.6b-v3", "YOLT_PARAKEET_MODEL"),
    # Use YouTube's own transcript when it has one: "any" (creator or auto-generated), "manual"
    # (creator-uploaded only), or "off". Auto-captions have no punctuation, so cut-point checks are
    # skipped for them and clips cut on pauses rather than sentences; they're faster (no Parakeet).
    "youtube_captions": Setting("any", "YOLT_YOUTUBE_CAPTIONS"),
    "highlightr_url": Setting("http://127.0.0.1:8000", "YOLT_HIGHLIGHTR_URL"),
    # An empty YOLT_LLM_ENDPOINT leaves Highlightr on its own configured LLM and turns speaker inference off.
    "llm_endpoint": Setting("", "YOLT_LLM_ENDPOINT"),
    "llm_model": Setting("", "YOLT_LLM_MODEL"),
    "llm_api_key": Setting("", "YOLT_LLM_API_KEY", secret=True),
    # OpenAI-compatible /v1/embeddings for semantic search. An empty YOLT_EMBED_URL turns semantic search
    # off; keyword search always works. Vectors record the model, so changing it re-embeds everything.
    "embed_url": Setting("", "YOLT_EMBED_URL"),
    "embed_model": Setting("Qwen/Qwen3-Embedding-8B", "YOLT_EMBED_MODEL"),
    "min_clip_seconds": Setting("20"),
    "max_clip_seconds": Setting("60"),
    "max_clips": Setting("8"),
    # Candidates Highlightr proposes per 10-minute window before ranking them all together. More gives
    # the ranker more to choose from, at one LLM call per window either way (bigger answers).
    "candidates_per_window": Setting("3"),
    # Default focus for clips, sent to Highlightr as its "preferred style": its own default
    # ("insightful, surprising, emotionally engaging") plus a hook, for short-form.
    "clip_instructions": Setting("insightful, surprising, emotionally engaging, with a strong hook in the first few seconds"),
    # Subscriptions: hours between checks of each channel (0: only when asked), and the shortest video
    # a check keeps, in minutes: once a day, 30 minutes.
    "subscription_check_hours": Setting("24"),
    "subscription_min_minutes": Setting("30"),
    # Labs switched off, as a comma list of their IDs (yolt/labs/__init__.py); empty runs every lab.
    "labs_disabled": Setting("", "YOLT_LABS_DISABLED"),
    # The feed's (TV's) weight for clips made in the last 3, 7, 30 and 90 days, then for any clip at all
    # (yolt/domain/feed.py).
    "feed_mix": Setting("50,30,10,5,5"),
}

# Columns update_run may set; everything else about a run is fixed at creation.
RUN_FIELDS = {"status", "stage", "stage_started_at", "progress", "detail", "error",
              "transcript_version_id", "params", "finished_at"}

# A run with its episode, channel and transcript source; repo/runs.py shapes it for the API.
RUN_SELECT = """
SELECT r.*, e.title AS episode_title, e.published_at, e.platform, e.video_id, e.source_url, e.duration_ms,
       p.name AS podcast, p.url AS podcast_url, tv.source AS transcript_source,
       (SELECT count(*) FROM clips c WHERE c.run_id = r.id) AS clip_count
FROM runs r
JOIN episodes e ON e.id = r.episode_id
LEFT JOIN podcasts p ON p.id = e.podcast_id
LEFT JOIN transcript_versions tv ON tv.id = r.transcript_version_id
"""


def _save_details(conn: psycopg.Connection, episode_id: int, video_id: str, meta: dict) -> None:
    podcast_id = None
    if meta.get("channel"):
        podcast_id = conn.execute(
            "INSERT INTO podcasts (platform, external_id, name, url) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (platform, external_id) DO UPDATE SET name = excluded.name, url = excluded.url "
            "RETURNING id",
            (PLATFORM, meta.get("channel_id") or meta.get("channel_url") or meta["channel"],
             meta["channel"], meta.get("channel_url")),
        ).fetchone()["id"]
    duration = meta.get("duration_seconds")
    conn.execute(
        "UPDATE episodes SET title = %s, published_at = %s, duration_ms = %s, podcast_id = %s, "
        "description = COALESCE(%s, description) WHERE id = %s",
        (meta.get("title") or f"YouTube video {video_id}", meta.get("published_at"),
         round(duration * 1000) if duration else None, podcast_id, meta.get("description"), episode_id),
    )


def _require(cursor: psycopg.Cursor, run_id: int) -> None:
    """A guarded write or check on a run matched nothing: the run is no longer the worker's to touch."""
    if cursor.rowcount == 0:
        raise RunCancelled(f"Run {run_id} was cancelled.")


def redact(url: str) -> str:
    """A connection string without its password, for error messages."""
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", url)


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any migrations not yet applied, in order. Returns their names."""
    applied = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())""")
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, path.stem))
            applied.append(path.stem)
    return applied


class Database:
    def __init__(self, url: str, *, max_size: int = 16, wait_seconds: float = 10.0,
                 setting_defaults: dict[str, str] | None = None):
        self.url, self.max_size, self.wait_seconds = url, max_size, wait_seconds
        self.defaults = {key: spec.default for key, spec in SETTINGS.items()} | (setting_defaults or {})
        self.pool: ConnectionPool | None = None
        self._saved: tuple[float, dict[str, str]] | None = None  # (read at, settings rows)
        self._saved_lock = threading.Lock()
        self.open()
        with self.connect() as conn:
            migrate(conn)

    def open(self) -> None:
        """Connect, unless already connected. A closed Database can be opened again."""
        if self.pool is not None and not self.pool.closed:
            return
        pool = ConnectionPool(
            self.url,
            kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
            min_size=1,
            max_size=self.max_size,
            open=False,
            name="yolt",
            # A pooled connection is checked before it's handed out, so after PostgreSQL restarts a
            # dead idle one is replaced instead of failing the next request or run step.
            check=ConnectionPool.check_connection,
        )
        try:
            pool.open(wait=True, timeout=self.wait_seconds)
        except PoolTimeout as error:
            pool.close()
            raise DatabaseUnavailable(
                f"Can't reach PostgreSQL at {redact(self.url)} (waited {self.wait_seconds:g}s). "
                "Start it with `make db`, or check YOLT_DATABASE_URL."
            ) from error
        self.pool = pool

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        """A pooled connection; the block is one transaction, committed on success."""
        with self.pool.connection() as conn:
            yield conn

    def schema_version(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"]

    # --- settings ---------------------------------------------------------

    def settings(self) -> dict[str, str]:
        """Effective settings: environment > saved value > default. Clearing a saved setting restores
        its default, while an environment variable set to empty explicitly overrides it.

        The saved rows are reused for SETTINGS_TTL seconds, so frequent reads cost one query. A read may take
        a pooled connection, so don't call this while holding one: API requests read Settings once, before
        taking theirs (api.request_settings)."""
        with self._saved_lock:
            cached = self._saved
        if cached is None or time.monotonic() - cached[0] > SETTINGS_TTL:
            with self.connect() as conn:
                rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
            cached = (time.monotonic(), rows)
            with self._saved_lock:
                self._saved = cached
        saved = cached[1]
        out = {}
        for key, spec in SETTINGS.items():
            env_value = os.environ.get(spec.env) if spec.env else None
            out[key] = env_value if env_value is not None else saved.get(key) or self.defaults[key]
        return out

    def env_overrides(self) -> set[str]:
        return {k for k, s in SETTINGS.items() if s.env and os.environ.get(s.env) is not None}

    def save_settings(self, values: dict[str, str]) -> None:
        unknown = values.keys() - SETTINGS.keys()
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
        with self.connect() as conn, conn.cursor() as cur:
            cleared = [k for k, v in values.items() if not v]  # back to the default
            if cleared:
                cur.execute("DELETE FROM settings WHERE key = ANY(%s)", (cleared,))
            if kept := [(k, v) for k, v in values.items() if v]:
                cur.executemany(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    kept,
                )
        with self._saved_lock:
            self._saved = None

    # --- embedding ----------------------------------------------------------

    def record_embedding(self, error: str | None) -> None:
        """Note how an embedding attempt went: its error, or None when it worked (clearing the last error)."""
        with self.connect() as conn:
            if error is None:
                conn.execute("UPDATE embedding_status SET last_error = NULL, last_error_at = NULL "
                             "WHERE last_error IS NOT NULL")
            else:
                conn.execute("UPDATE embedding_status SET last_error = %s, last_error_at = now()", (error,))

    # --- runs and the queue -----------------------------------------------

    def submit(self, video_id: str, instructions: str, meta: dict, options: dict | None = None) -> int:
        """Queue a new run for a video, creating or updating its episode from `meta` (youtube.metadata), and
        return its id. `options` are the run's own clip options (api.RunOptions), each overriding its setting.
        A video may have any number of runs, with the same instructions and options or not; each reuses its
        transcript."""
        with self.connect() as conn:
            episode_id = conn.execute(
                "INSERT INTO episodes (platform, video_id, source_url) VALUES (%s, %s, %s) "
                "ON CONFLICT (platform, video_id) DO UPDATE SET platform = excluded.platform RETURNING id",
                (PLATFORM, video_id, f"https://www.youtube.com/watch?v={video_id}"),
            ).fetchone()["id"]
            _save_details(conn, episode_id, video_id, meta)
            return conn.execute(
                "INSERT INTO runs (episode_id, instructions, options) VALUES (%s, %s, %s) RETURNING id",
                (episode_id, instructions, Jsonb(options or {})),
            ).fetchone()["id"]

    def queue_run(self, run_id: int) -> bool:
        """Queue a failed or cancelled run again. False if it isn't one: a second Retry click after the
        worker claimed the run must not reset it under the worker."""
        with self.connect() as conn:
            return conn.execute(
                "UPDATE runs SET status = 'queued', stage = NULL, step = NULL, step_started_at = NULL, "
                "progress = NULL, detail = NULL, error = NULL, finished_at = NULL, queued_at = now(), interruptions = 0, "
                # A Retry asks Highlightr afresh: the job it had may be what hung or failed. (A restart, which
                # doesn't come through here, picks that job up again.)
                "params = params - 'highlightr_job_id' "
                "WHERE id = %s AND status IN ('failed', 'cancelled')",
                (run_id,),
            ).rowcount == 1

    def claim_next_run(self) -> int | None:
        """Mark the oldest queued run as processing and return its id."""
        with self.connect() as conn:
            row = conn.execute(
                """UPDATE runs SET status = 'processing', attempts = attempts + 1, stage = NULL, step = NULL,
                       step_started_at = NULL, progress = NULL, detail = NULL, error = NULL
                   WHERE id = (SELECT id FROM runs WHERE status = 'queued'
                               ORDER BY queued_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
                   RETURNING id"""
            ).fetchone()
        return row["id"] if row else None

    # --- clearing the catalogue ---------------------------------------------

    # Everything made from videos, in one TRUNCATE (so their references to each other don't matter): what
    # "Clear all" deletes. Settings, subscriptions, the videos they found and the channels those name stay.
    # A table added later that references one of these makes the TRUNCATE fail until it's listed here.
    CLEARED_TABLES = ("feed_plays", "clip_vectors", "passage_vectors", "passages", "clip_quotes",
                      "compilation_render_positions", "compilation_renders", "compilation_items", "compilations",
                      "clips", "episode_speakers", "speakers", "runs", "transcript_words", "transcript_versions",
                      "episodes")
    CLEAR_LOCK_TIMEOUT = "5s"  # waiting longer than this for a busy table answers 409 instead

    def clear_catalogue(self) -> dict[str, int]:
        """Delete every episode and all made from it: transcripts, runs (queued and processing too), clips
        and their vectors, compilations and render records, speakers and TV plays. Returns how many episodes,
        runs, clips and compilations there were. Channels no subscription or found video names go too.

        A run the worker is on stops at its next write (each checks the run is still there), as if it was
        cancelled. Raises psycopg.errors.LockNotAvailable if a table stayed busy for CLEAR_LOCK_TIMEOUT."""
        with self.connect() as conn:
            conn.execute(f"SET LOCAL lock_timeout = '{self.CLEAR_LOCK_TIMEOUT}'")
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM episodes) AS episodes, (SELECT count(*) FROM runs) AS runs, "
                "(SELECT count(*) FROM clips) AS clips, "
                "(SELECT count(DISTINCT title) FROM compilations) AS compilations").fetchone()
            conn.execute(f"TRUNCATE {', '.join(self.CLEARED_TABLES)}")
            conn.execute("DELETE FROM podcasts p WHERE NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.podcast_id = p.id) "
                         "AND NOT EXISTS (SELECT 1 FROM channel_videos v WHERE v.podcast_id = p.id)")
            conn.execute("UPDATE embedding_status SET last_error = NULL, last_error_at = NULL")
        return dict(counts)

    def requeue_interrupted(self) -> int:
        """Put runs a worker was processing when it stopped unexpectedly back in the queue (Yolt runs one
        worker, so whatever is still processing has none). A run this has happened to MAX_INTERRUPTIONS times
        since it was last queued by Retry fails instead, so a run that crashes the server can't crash it on
        every start."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET status = 'failed', interruptions = interruptions + 1, progress = NULL, detail = NULL, "
                "error = %(error)s, finished_at = now() WHERE status = 'processing' AND interruptions + 1 >= %(max)s",
                {"max": MAX_INTERRUPTIONS, "error": INTERRUPTED_ERROR},
            )
            return conn.execute(
                "UPDATE runs SET status = 'queued', interruptions = interruptions + 1, stage = NULL, step = NULL, "
                "step_started_at = NULL, progress = NULL, detail = NULL WHERE status = 'processing'"
            ).rowcount

    def release_run(self, run_id: int, attempt: int) -> bool:
        """A clean shutdown: put the run this worker was processing back at the front of the queue, without
        counting it as an interruption. False if it's no longer this worker's (finished, cancelled, ...)."""
        with self.connect() as conn:
            return conn.execute(
                "UPDATE runs SET status = 'queued', stage = NULL, step = NULL, step_started_at = NULL, "
                "progress = NULL, detail = NULL WHERE id = %s AND status = 'processing' AND attempts = %s",
                (run_id, attempt),
            ).rowcount == 1

    # The worker's writes to a run below only land while the run is still active, and raise
    # RunCancelled otherwise, so a cancelled run is never un-cancelled, finished or failed, and every
    # progress update doubles as a check that the work is still wanted.
    #
    # update_run and finish_run take the `attempt` the worker claimed (runs.attempts): then they only
    # touch the run while it's processing that attempt, not once it was cancelled and queued again, or
    # claimed by another worker since. Without one (writing a run no worker has, e.g. seeding a
    # catalogue) a queued run may be written too.

    @staticmethod
    def _active(attempt: int | None) -> tuple[str, tuple]:
        if attempt is None:
            return "status IN ('queued', 'processing')", ()
        return "status = 'processing' AND attempts = %s", (attempt,)

    @staticmethod
    def _processing(attempt: int | None) -> tuple[str, tuple]:
        """The run is processing (that attempt of it, if given): what stage, step and cancel checks need."""
        if attempt is None:
            return "status = 'processing'", ()
        return "status = 'processing' AND attempts = %s", (attempt,)

    def update_run(self, run_id: int, *, attempt: int | None = None, **fields) -> None:
        unknown = fields.keys() - RUN_FIELDS
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
        if not fields:
            return
        if "params" in fields:
            fields["params"] = Jsonb(fields["params"])
        columns = ", ".join(f"{k} = %s" for k in fields)
        active, args = self._active(attempt)
        with self.connect() as conn:
            _require(conn.execute(f"UPDATE runs SET {columns} WHERE id = %s AND {active}",
                                  (*fields.values(), run_id, *args)), run_id)

    def set_stage(self, run_id: int, stage: str, detail: str | None = None, *, attempt: int | None = None) -> None:
        active, args = self._processing(attempt)
        with self.connect() as conn:
            _require(conn.execute(
                "UPDATE runs SET stage = %s, stage_started_at = now(), step = NULL, step_started_at = NULL, "
                f"progress = NULL, detail = %s, error = NULL WHERE id = %s AND {active}",
                (stage, detail, run_id, *args),
            ), run_id)

    def set_step(self, run_id: int, step: str, *, progress: float | None = None, detail: str | None = None,
                 attempt: int | None = None) -> None:
        """Move to a step within the current stage; its clock restarts only when the step changes."""
        active, args = self._processing(attempt)
        with self.connect() as conn:
            _require(conn.execute(
                "UPDATE runs SET step_started_at = CASE WHEN step IS DISTINCT FROM %s THEN now() "
                f"ELSE step_started_at END, step = %s, progress = %s, detail = %s WHERE id = %s AND {active}",
                (step, step, progress, detail, run_id, *args),
            ), run_id)

    def ensure_processing(self, run_id: int, *, attempt: int | None = None) -> None:
        """Raise RunCancelled unless the run is still processing (that attempt of it, if given)."""
        active, args = self._processing(attempt)
        with self.connect() as conn:
            _require(conn.execute(f"SELECT 1 FROM runs WHERE id = %s AND {active}", (run_id, *args)), run_id)

    def fail_run(self, run_id: int, error: str, *, attempt: int | None = None) -> None:
        """Mark a processing run (that attempt of it, if given) failed. A run cancelled, or queued again,
        meanwhile stays as it is."""
        active, args = self._processing(attempt)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s AND {active}",
                         (error, run_id, *args))

    def cancel_run(self, run_id: int) -> bool:
        """Cancel a queued or processing run; False if it isn't one. The worker notices at its next
        write or check and stops; the stage it reached stays, as for a failed run."""
        with self.connect() as conn:
            return conn.execute(
                "UPDATE runs SET status = 'cancelled', progress = NULL, detail = NULL, error = NULL, "
                "finished_at = now() WHERE id = %s AND status IN ('queued', 'processing')",
                (run_id,),
            ).rowcount == 1

    def run(self, run_id: int) -> dict | None:
        with self.connect() as conn:
            return conn.execute(f"{RUN_SELECT} WHERE r.id = %s", (run_id,)).fetchone()

    # --- episodes ---------------------------------------------------------

    def save_episode_details(self, episode_id: int, meta: dict) -> None:
        """Refresh what the video's metadata says: title, channel, publish time, length."""
        with self.connect() as conn:
            video_id = conn.execute("SELECT video_id FROM episodes WHERE id = %s", (episode_id,)).fetchone()["video_id"]
            _save_details(conn, episode_id, video_id, meta)

    # --- transcripts ------------------------------------------------------

    def latest_transcript_id(self, episode_id: int, sources: tuple[str, ...] | None = None) -> int | None:
        """The episode's newest transcript, of one of `sources` (parakeet, youtube, youtube_auto) if given."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM transcript_versions WHERE episode_id = %s "
                "AND (%s::text[] IS NULL OR source = ANY(%s::text[])) ORDER BY id DESC LIMIT 1",
                (episode_id, sources and list(sources), sources and list(sources)),
            ).fetchone()
        return row["id"] if row else None

    def save_transcript(self, episode_id: int, words: list[Word], *, source: str, model: str | None,
                        language: str | None, duration_ms: int | None) -> int:
        """Store a transcript and all its words; the same words twice is one version."""
        if not words:
            raise ValueError("A transcript needs at least one word.")
        digest = timing_digest(words)
        with self.connect() as conn:
            row = conn.execute(
                "INSERT INTO transcript_versions (episode_id, source, model, language, timing_digest, word_count, "
                "duration_ms, full_text, punctuated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (episode_id, timing_digest) DO NOTHING RETURNING id",
                (episode_id, source, model, language, digest, len(words), duration_ms or None, full_text(words),
                 punctuated(words)),
            ).fetchone()
            if row is None:
                return conn.execute(
                    "SELECT id FROM transcript_versions WHERE episode_id = %s AND timing_digest = %s",
                    (episode_id, digest),
                ).fetchone()["id"]
            version_id = row["id"]
            with conn.cursor() as cur, cur.copy(
                "COPY transcript_words (transcript_version_id, seq, word, start_ms, end_ms) FROM STDIN"
            ) as copy:
                for w in words:
                    copy.write_row((version_id, w.seq, w.word, w.start_ms, w.end_ms))
        return version_id

    def words(self, version_id: int, before_ms: int | None = None) -> list[Word]:
        """A transcript's words; only those starting before `before_ms` if given (its opening)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT seq, word, start_ms, end_ms FROM transcript_words "
                "WHERE transcript_version_id = %s AND (%s::integer IS NULL OR start_ms < %s) ORDER BY seq",
                (version_id, before_ms, before_ms),
            ).fetchall()
        return [Word(**r) for r in rows]

    # --- clips ------------------------------------------------------------

    def finish_run(self, run_id: int, clips: list[NewClip], *, attempt: int | None = None) -> None:
        """Store a run's clips, quotes and passages and mark it ready, all at once.

        Raises RunCancelled, storing nothing, if the run was cancelled (or, given the worker's
        `attempt`, queued or claimed again since). The run row stays locked until this commits, so a
        cancel arriving meanwhile finds the run ready and is refused."""
        active, args = self._active(attempt)
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM clips WHERE run_id = %s LIMIT 1", (run_id,)).fetchone():
                raise RuntimeError(f"Run {run_id} already has clips; clips are never replaced.")
            _require(conn.execute(f"SELECT 1 FROM runs WHERE id = %s AND {active} FOR UPDATE",
                                  (run_id, *args)), run_id)
            for clip in clips:
                clip_id = conn.execute(
                    "INSERT INTO clips (slug, run_id, local_clip_id, rank, title, summary, start_ms, end_ms, "
                    "first_word_seq, last_word_seq, overall_score, details, has_thumb) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (clip.slug, run_id, clip.local_clip_id, clip.rank, clip.title, clip.summary, clip.start_ms,
                     clip.end_ms, clip.first_word_seq, clip.last_word_seq, clip.overall_score,
                     Jsonb(clip.details), clip.has_thumb),
                ).fetchone()["id"]
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO clip_quotes (clip_id, ordinal, quote_text, first_word_seq, last_word_seq) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [(clip_id, q.ordinal, q.text, q.first_word_seq, q.last_word_seq) for q in clip.quotes],
                    )
                    cur.executemany(
                        "INSERT INTO passages (clip_id, segmentation_version, first_word_seq, last_word_seq, "
                        "text, text_digest) VALUES (%s, %s, %s, %s, %s, %s)",
                        [(clip_id, PASSAGE_VERSION, p.first_word_seq, p.last_word_seq, p.text, p.text_digest)
                         for p in clip.passages],
                    )
            conn.execute(
                "UPDATE runs SET status = 'ready', stage = NULL, step = NULL, step_started_at = NULL, "
                "progress = NULL, detail = NULL, error = NULL, finished_at = now() WHERE id = %s",
                (run_id,),
            )

    # --- speakers ---------------------------------------------------------

    def speakers_pending(self, episode_id: int | None = None, *, exclude: tuple[int, ...] = ()) -> dict | None:
        """An episode with a transcript whose speakers haven't been inferred yet (this one, or any).

        The one tried longest ago comes first, so a failure moves on to the next episode instead of
        retrying the same one; episodes that gave MAX_SPEAKER_ATTEMPTS unusable answers are skipped."""
        with self.connect() as conn:
            return conn.execute(
                """SELECT e.id, e.title, e.description, p.name AS channel,
                          (SELECT max(tv.id) FROM transcript_versions tv WHERE tv.episode_id = e.id) AS transcript_id
                   FROM episodes e LEFT JOIN podcasts p ON p.id = e.podcast_id
                   WHERE e.speakers_inferred_at IS NULL AND e.speakers_attempts < %(max)s
                     AND EXISTS (SELECT 1 FROM transcript_versions tv WHERE tv.episode_id = e.id)
                     AND (%(episode)s::bigint IS NULL OR e.id = %(episode)s)
                     AND e.id <> ALL(%(exclude)s::bigint[])
                   ORDER BY e.speakers_tried_at NULLS FIRST, e.id LIMIT 1""",
                {"episode": episode_id, "max": MAX_SPEAKER_ATTEMPTS, "exclude": list(exclude)}).fetchone()

    def speakers_failed(self, episode_id: int, *, counts: bool) -> None:
        """Note a failed inference. `counts` is false when the LLM itself was unavailable."""
        with self.connect() as conn:
            conn.execute("UPDATE episodes SET speakers_tried_at = now(), "
                         "speakers_attempts = speakers_attempts + %s WHERE id = %s", (1 if counts else 0, episode_id))

    def save_inferred_speakers(self, episode_id: int, people: dict[str, list[str]], method: str) -> None:
        """Store an inference as unverified speakers. Speakers someone confirmed are never replaced."""
        with self.connect() as conn:
            verified = conn.execute("SELECT 1 FROM episode_speakers WHERE episode_id = %s AND verified LIMIT 1",
                                    (episode_id,)).fetchone()
            if not verified:
                conn.execute("DELETE FROM episode_speakers WHERE episode_id = %s", (episode_id,))
                for role, names in (("host", people.get("hosts", [])), ("guest", people.get("guests", []))):
                    for name in names:
                        _link_speaker(conn, episode_id, name, role, method, verified=False)
            conn.execute("UPDATE episodes SET speakers_inferred_at = now() WHERE id = %s", (episode_id,))

    def set_speakers(self, episode_id: int, people: list[tuple[str, str]]) -> None:
        """Replace an episode's speakers with confirmed ones: [(name, role)]."""
        with self.connect() as conn:
            conn.execute("DELETE FROM episode_speakers WHERE episode_id = %s", (episode_id,))
            for name, role in people:
                _link_speaker(conn, episode_id, name, role, "manual", verified=True)
            conn.execute("UPDATE episodes SET speakers_inferred_at = COALESCE(speakers_inferred_at, now()) "
                         "WHERE id = %s", (episode_id,))


def _link_speaker(conn: psycopg.Connection, episode_id: int, name: str, role: str, method: str, *,
                  verified: bool) -> None:
    """Attach a person (found or created by case-insensitive name) to an episode."""
    conn.execute("INSERT INTO speakers (name) VALUES (%s) ON CONFLICT ((lower(name))) DO NOTHING", (name,))
    speaker_id = conn.execute("SELECT id FROM speakers WHERE lower(name) = lower(%s)", (name,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO episode_speakers (episode_id, speaker_id, role, attribution_method, verified) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (episode_id, speaker_id) DO NOTHING",
        (episode_id, speaker_id, role, method, verified))


class WorkerLock:
    """Held by the one worker allowed to process a database's runs; released when its connection closes,
    which a PostgreSQL restart does too, so the worker checks it still holds it (`ensure`) before each run."""

    def __init__(self, url: str):
        self.url = url
        self.conn: psycopg.Connection | None = None

    def acquire(self) -> bool:
        conn = psycopg.connect(self.url, autocommit=True, connect_timeout=10)
        if conn.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,)).fetchone()[0]:
            self.conn = conn
            return True
        conn.close()
        return False

    def held(self) -> bool:
        """Whether the lock's connection is alive and still holds it."""
        if self.conn is None or self.conn.closed:
            return False
        try:
            return self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                "AND granted AND classid = 0 AND objid = %s::bigint::oid AND objsubid = 1)", (WORKER_LOCK,),
            ).fetchone()[0]
        except psycopg.Error:
            return False

    def ensure(self) -> bool:
        """Still hold the lock, taking it again if it was lost (e.g. PostgreSQL restarted). False while
        the database can't be reached or another server took it meanwhile."""
        if self.held():
            return True
        self.release()
        try:
            return self.acquire()
        except psycopg.Error:
            return False

    def release(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
