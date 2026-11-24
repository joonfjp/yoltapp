"""Background worker: YouTube link -> transcript -> Highlightr -> clips.

The transcript comes from YouTube's own captions when the video has them, and
otherwise from downloading the audio and running Parakeet. A video is
transcribed once; later runs on it (new instructions, retries) reuse that, if
the captions setting allows its source ("off" only reuses Parakeet's, "manual"
also creator captions). Audio Parakeet failed on is kept for a while, so a retry
doesn't download it again; so is the Highlightr job, which a retry or restart
picks up again if Highlightr still has it.

The queue is the runs table. One worker claims one run at a time, oldest
first, so a machine never runs two transcriptions at once; a database-wide lock
keeps a second Yolt server on the same database from starting another worker.
The worker checks it still holds the lock before each run (a PostgreSQL restart
drops it): it takes it back if it can, and otherwise leaves the queue alone.

A run can be cancelled while it's queued or processing. Each of the worker's writes to the run
checks it's still active, so the worker drops a cancelled run at its next progress update: within
a second or so while downloading, converting the audio or waiting on Highlightr, but only once
Parakeet answers while transcribing (that request can't be interrupted; the transcript is still
kept for later runs).

Once a run is ready its clips and passages are embedded for semantic search, and
the episode's speakers are inferred (once per episode). Both are best effort: if a
service is down the run stays ready, and the worker catches up whenever it's idle.
Idle with new vectors, it also clusters the themes the home page and Search show by
default, so the first look at them doesn't wait for it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from . import ingest, services, speakers, transcript, youtube
from .subscriptions import PAUSE_SECONDS, Collector
from .db import Database, RunCancelled, WorkerLock
from .embed import embed_pending
from .embeddings import EmbeddingService, EmbeddingUnavailable
from .ingest import Word
from .repo import themes as theme_repo

log = logging.getLogger("yolt.pipeline")

# Captions that stop well before the video ends (e.g. only the intro was
# captioned) are ignored in favor of a full Parakeet transcript.
MIN_CAPTION_COVERAGE = 0.6
# The transcripts each captions setting may reuse (README: "off" always transcribes, "manual" uses
# creator captions only); "any" reuses whichever is newest.
REUSABLE_SOURCES = {"off": ("parakeet",), "manual": ("parakeet", "youtube")}
AUDIO_KEEP_SECONDS = 24 * 3600  # audio kept for a retry after Parakeet failed is deleted after this
SWEEP_SECONDS = 3600  # how often an idle worker deletes expired audio and abandoned work folders
SECRET_FIELDS = {"llm_api_key"}
# Highlightr's job stages, as the steps of Yolt's "identifying" stage. Only discovery reports progress
# (windows done); the others report which step they're on, so the UI shows that and its elapsed time.
HIGHLIGHTR_STEPS = {"queued": "waiting", "identifying": "discovering", "verifying_topics": "verifying_topics",
                    "global_ranking": "ranking", "enriching_metadata": "writing_headlines"}
BACKFILL_BATCH = 64  # pending vectors embedded per idle tick
BACKFILL_CHECK_SECONDS = 60  # how often an idle worker looks for pending vectors
BACKFILL_RETRY_SECONDS = 300  # after the embedding server failed


def highlightr_fields(
    settings: dict[str, str], source_id: str, title: str | None, instructions: str | None = None
) -> dict[str, str]:
    fields = {
        "source_id": source_id,
        "min_clip_duration_seconds": settings["min_clip_seconds"],
        "max_clip_duration_seconds": settings["max_clip_seconds"],
        "candidates_per_window": settings["candidates_per_window"],
        # RipeClips' defaults for short-form discovery.
        "dedupe_overlap_threshold": "0.65",
        "admission_strictness": "0.7",
    }
    if title:
        fields["title"] = title
    if settings["max_clips"]:  # empty only through an empty environment variable: no limit
        fields["max_clips"] = settings["max_clips"]
    if instructions:
        # Highlightr puts this in its discovery and ranking prompts as
        # "Preferred style: ...".
        fields["style"] = instructions
    for key in ("llm_endpoint", "llm_model", "llm_api_key"):
        if settings[key]:
            fields[key] = settings[key]
    return fields


class Worker:
    def __init__(
        self,
        db: Database,
        data_dir: Path,
        *,
        embeddings: EmbeddingService | None = None,
        client_factory: Callable[[], httpx.Client] = httpx.Client,
        poll_seconds: float = services.POLL_SECONDS,
        idle_seconds: float = 5.0,
        subscription_pause_seconds: float = PAUSE_SECONDS,
    ):
        self.db = db
        # Channel subscriptions are checked on their own thread, started with this worker's lock.
        self.subscriptions = Collector(db, pause_seconds=subscription_pause_seconds, active=lambda: self.active,
                                       queued=lambda: self.wake())
        self.embeddings = embeddings or EmbeddingService(db.settings, client_factory)
        self._backfill_after = 0.0
        self._speakers_after = 0.0
        # The UI's default themes are clustered ahead once the worker is idle: at start (the cache is in memory),
        # and after anything new is embedded (which changes the clustering).
        self._themes_due = True
        self._lock = WorkerLock(db.url)
        self.active = False  # whether this server's worker is the one processing runs
        self._unrecorded: dict[int, tuple[str, int | None]] = {}  # failures not saved yet: (error, attempt)
        self._swept_at = 0.0
        self._current: tuple[int, int] | None = None  # (run id, attempt) being processed
        self.data_dir = data_dir
        self.client_factory = client_factory
        self.poll_seconds = poll_seconds
        self.idle_seconds = idle_seconds
        self.info_cache = youtube.InfoCache()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="yolt-worker", daemon=True)

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def audio_dir(self) -> Path:
        """Complete audio downloads Parakeet hasn't transcribed yet, one per video."""
        return self.data_dir / "work" / "audio"

    def start(self) -> None:
        if not self._lock.acquire():
            log.warning("another Yolt server is already processing runs for this database; this one won't")
            return
        self.active = True
        # Resume anything a restart interrupted. Safe: no other worker can hold runs now.
        self.db.requeue_interrupted()
        self._sweep()
        self.thread.start()
        self.subscriptions.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._wake.set()
        self.subscriptions.stop(timeout)
        if self.thread.is_alive():
            self.thread.join(timeout)
        current = self._current
        if current is not None:  # still busy (e.g. waiting on Parakeet): resume it on the next start
            try:
                self.db.release_run(*current)
            except Exception:  # noqa: BLE001 - then the next start counts it as an interruption
                log.exception("couldn't put run %s back in the queue", current[0])
        if not self.thread.is_alive():
            self._lock.release()  # a thread still busy keeps it until the process exits

    def wake(self) -> None:
        """A run was queued; check the queue now rather than at the next idle tick."""
        self._wake.set()

    def embeddings_changed(self) -> None:
        """Settings now name a different embedding server or model: catch up now, not after a backoff."""
        self._backfill_after = 0.0
        self.wake()

    def clear_files(self) -> int:
        """After the catalogue was cleared: delete every clip thumbnail and the audio kept for retries.
        Returns how many thumbnails there were. A run still finishing removes its own if its clips can't be saved."""
        removed = 0
        for path in self.thumbs_dir.glob("*.jpg"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass  # e.g. already gone
        for path in self.audio_dir.glob("*.mp3"):
            path.unlink(missing_ok=True)
        return removed

    def enqueue(self, run_id: int) -> bool:
        """Queue a failed or cancelled run again; False (and nothing changes) if it's no longer one."""
        if not self.db.queue_run(run_id):
            return False
        self.wake()
        return True

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - e.g. the database restarting; the thread must survive it
                log.exception("worker error; trying again shortly")
                self._wake.wait(self.idle_seconds)
                self._wake.clear()

    def _holds_lock(self) -> bool:
        """Whether this server still holds the worker lock, taking it back if a database restart dropped
        it. While another server holds it (or the database is down) this one only serves."""
        if self._lock.ensure():
            if not self.active:
                self.db.requeue_interrupted()  # no worker held the lock, so whatever is processing has none
                self.active = True
                log.warning("holding the worker lock again; processing runs")
            return True
        if self.active:
            self.active = False
            log.warning("lost the worker lock (did PostgreSQL restart?) and can't take it back: another Yolt "
                        "server has it or the database is down; not processing runs until it's free")
        return False

    def _tick(self) -> None:
        # Failures first: taking the lock back below re-queues whatever is still processing, and a run that
        # failed while the database was down should be recorded as failed, not run again. Only that attempt
        # is touched, so it's safe even if another server has the run now.
        for run_id, (error, attempt) in list(self._unrecorded.items()):
            self.db.fail_run(run_id, error, attempt=attempt)
            del self._unrecorded[run_id]
        if not self._holds_lock():
            self._wake.wait(self.idle_seconds)
            self._wake.clear()
            return
        run_id = self.db.claim_next_run()
        if run_id is None:
            if time.monotonic() - self._swept_at > SWEEP_SECONDS:
                self._sweep()
            more = self._backfill()
            more = self._speaker_backfill() or more
            if not more and self._themes_due:  # not between backfill batches: each changes the vectors again
                self._warm_themes()
            if not more:  # keep going while there's a backlog; runs still come first
                self._wake.wait(self.idle_seconds)
                self._wake.clear()
            return
        try:
            self.process(run_id)
        except services.Interrupted:
            log.info("run %s interrupted by shutdown; it resumes when Yolt starts again", run_id)
            current = self._current
            if current is not None:
                self.db.release_run(*current)
        except RunCancelled:
            log.info("run %s cancelled", run_id)
        except Exception as error:  # noqa: BLE001 - surfaced to the user
            log.exception("run %s failed", run_id)
            message = str(error) or repr(error)
            attempt = self._current[1] if self._current else None
            try:
                self.db.fail_run(run_id, message, attempt=attempt)
            except Exception:  # noqa: BLE001 - recorded on a later tick, once the database is back
                log.exception("couldn't record run %s as failed yet", run_id)
                self._unrecorded[run_id] = (message, attempt)
        finally:
            self._current = None

    def process(self, run_id: int) -> None:
        run = self.db.run(run_id)
        if not run:
            return
        # The run's own clip options (POST /runs `options`) override their settings for this run.
        settings = {**self.db.settings(), **{key: str(value) for key, value in (run["options"] or {}).items()}}
        video_id = run["video_id"]
        attempt = run["attempts"]  # the claim this worker holds; writes stop landing once it's not current
        self._current = (run_id, attempt)
        # Per attempt: a stale attempt (another server took the run over) cleaning up can't delete the new one's.
        workdir = self.data_dir / "work" / f"{run_id}-{attempt}"
        workdir.mkdir(parents=True, exist_ok=True)
        self._sweep()
        try:
            with self.client_factory() as client:
                self.db.set_stage(run_id, "fetching", attempt=attempt)
                info = self.info_cache.get(video_id)
                meta = youtube.metadata(info)
                self.db.save_episode_details(run["episode_id"], meta)

                sources = REUSABLE_SOURCES.get(settings["youtube_captions"])
                version_id = run["transcript_version_id"]
                if version_id is None or (sources and run["transcript_source"] not in sources):
                    version_id = self.db.latest_transcript_id(run["episode_id"], sources)
                if version_id is None:
                    version_id = self._from_captions(run, info, settings) or self._transcribe(
                        run, settings, client, workdir
                    )
                self.db.update_run(run_id, attempt=attempt, transcript_version_id=version_id)
                words = self.db.words(version_id)
                srt = transcript.rows_to_srt(transcript.make_rows(ingest.as_seconds(words)))

                self.db.set_stage(run_id, "identifying", attempt=attempt)
                fields = highlightr_fields(settings, video_id, meta["title"], run["instructions"])
                public = {k: v for k, v in fields.items() if k not in SECRET_FIELDS}
                # The job an earlier attempt submitted is picked up again only if it was given the same
                # transcript and settings (the API key aside).
                inputs = ingest.sha256(json.dumps([public, srt], sort_keys=True))
                earlier = run["params"] or {}
                job_id = earlier.get("highlightr_job_id") if earlier.get("highlightr_inputs") == inputs else None
                public = {**public, "highlightr_inputs": inputs}
                self.db.update_run(run_id, attempt=attempt,
                                   params={**public, "highlightr_job_id": job_id} if job_id else public)
                last = [0.0]

                current = [None]

                def on_windows(done: int, total: int, stage: str) -> None:
                    step = HIGHLIGHTR_STEPS.get(stage, current[0] or "discovering")
                    if step == current[0] and time.monotonic() - last[0] < 1 and done < total:
                        return  # discovery progress at most once a second; step changes at once
                    last[0], current[0] = time.monotonic(), step
                    discovering = step == "discovering"
                    self.db.set_step(run_id, step, attempt=attempt,
                                     progress=done / total if discovering and total else None,
                                     detail=f"{done}/{total} windows" if total else None)

                def on_job(used: str) -> None:
                    log.info("run %s: Highlightr job %s%s", run_id, used,
                             " (picked up again)" if used == job_id else "")
                    self.db.update_run(run_id, attempt=attempt, params={**public, "highlightr_job_id": used})

                def should_stop() -> bool:
                    self.db.ensure_processing(run_id, attempt=attempt)  # cancelled: stop waiting (Highlightr can't be told)
                    return self._stopping.is_set()

                result = services.identify_highlights(
                    client, settings["highlightr_url"], srt, fields,
                    on_progress=on_windows, poll_seconds=self.poll_seconds, should_stop=should_stop,
                    on_job=on_job, job_id=job_id,
                )
                clips = ingest.build_clips(result, words)
                # Hours may have passed since `info` was fetched: expired sheet URLs are fetched afresh.
                frames = youtube.StoryboardFrames(info, workdir, client, refresh=lambda: youtube.fetch_info(video_id))
                thumbs: list[Path] = []
                try:
                    for n, clip in enumerate(clips):
                        self.db.set_step(run_id, "saving", progress=n / len(clips), detail=f"{n}/{len(clips)} clips",
                                         attempt=attempt)
                        start, end = clip.start_ms / 1000, clip.end_ms / 1000
                        at = start + min(3.0, (end - start) / 4)
                        path = self.thumbs_dir / f"{clip.slug}.jpg"
                        clip.has_thumb = frames.frame(at, path)
                        thumbs.append(path)
                    self.db.finish_run(run_id, clips, attempt=attempt)
                except BaseException:
                    for path in thumbs:  # no clip will ever point at these
                        path.unlink(missing_ok=True)
                    raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        self._embed_run(run_id)
        self._infer_speakers(run["episode_id"], known=(version_id, words))

    def _infer_speakers(self, episode_id: int | None = None, known: tuple[int, list[Word]] | None = None) -> bool:
        """Infer speakers for this episode, or for any pending one. True if one was done. Never fails a run.

        `known` is a transcript's (version id, words) the caller has in memory already; otherwise only the
        opening minutes the inference looks at are read."""
        pending = None
        try:
            pending = self.db.speakers_pending(episode_id)
            if pending is None:
                return False
            settings = self.db.settings()
            if not settings["llm_endpoint"].strip():
                return False  # Highlightr's own LLM isn't one Yolt can call
            if known and known[0] == pending["transcript_id"]:
                words = known[1]
            else:
                words = self.db.words(pending["transcript_id"], before_ms=speakers.EXCERPT_SECONDS * 1000)
            with self.client_factory() as client:
                people = speakers.infer(client, settings, title=pending["title"], channel=pending["channel"],
                                        description=pending["description"], words=words)
            self.db.save_inferred_speakers(pending["id"], people, speakers.METHOD)
            log.info("episode %s: %s hosts, %s guests", pending["id"], len(people["hosts"]), len(people["guests"]))
            return True
        except speakers.SpeakersUnavailable as error:
            log.warning("episode %s: speakers not inferred (%s)", pending and pending["id"], error)
            self._speaker_failed(pending, counts=not error.transient)
            if not error.transient:
                return True  # the LLM works; the next pending episode can go right away
        except Exception:  # noqa: BLE001 - speakers are best effort
            log.exception("speaker inference failed")
            self._speaker_failed(pending, counts=False)
        self._speakers_after = time.monotonic() + BACKFILL_RETRY_SECONDS
        return False

    def _speaker_failed(self, pending: dict | None, *, counts: bool) -> None:
        if pending is None:
            return
        try:
            self.db.speakers_failed(pending["id"], counts=counts)
        except Exception:  # noqa: BLE001
            log.exception("couldn't record the failed speaker inference")

    def _speaker_backfill(self) -> bool:
        """Infer speakers for one pending episode, at most every BACKFILL_CHECK_SECONDS when there are none."""
        now = time.monotonic()
        if self._stopping.is_set() or now < self._speakers_after:
            return False
        if self._infer_speakers():
            return True
        self._speakers_after = max(self._speakers_after, now + BACKFILL_CHECK_SECONDS)
        return False

    def _embed_run(self, run_id: int) -> None:
        """Make a ready run's clips searchable by meaning. Never fails the run."""
        try:
            if not self.embeddings.configured:
                return
            done = embed_pending(self.db, self.embeddings, run_id=run_id)
            log.info("run %s: embedded %s clips, %s passages", run_id, done.clips, done.passages)
            self._themes_due = self._themes_due or bool(done.total)
        except EmbeddingUnavailable as error:
            log.warning("run %s: not embedded yet (%s); will retry when idle", run_id, error)
            self._backfill_after = time.monotonic() + BACKFILL_RETRY_SECONDS
        except Exception:  # noqa: BLE001 - embedding is best effort
            log.exception("run %s: embedding failed", run_id)
            self._backfill_after = time.monotonic() + BACKFILL_RETRY_SECONDS

    def _warm_themes(self) -> None:
        """Cluster the themes the home page and Search show by default for the vectors as they are now, so the
        first look at them needn't wait (a second or more on a large catalogue). Best effort: if it fails, the
        first request clusters them, as it would anyway."""
        self._themes_due = False
        if self._stopping.is_set():
            return
        try:
            embeddings = self.embeddings.pinned(self.db.settings())  # read before taking the connection
            if not embeddings.configured:
                return
            started = time.monotonic()
            with self.db.connect() as conn:
                built = theme_repo.warm(conn, embeddings)
            if built:
                log.info("themes ready for k=%s (%.1fs)", ", ".join(map(str, built)), time.monotonic() - started)
        except Exception:  # noqa: BLE001 - e.g. the database restarting; GET /themes clusters them instead
            log.exception("couldn't cluster the themes ahead")

    def _backfill(self) -> bool:
        """Embed a batch of anything still pending. True if there may be more right away."""
        now = time.monotonic()
        if self._stopping.is_set() or now < self._backfill_after:
            return False
        try:
            if not self.embeddings.configured:
                self._backfill_after = now + BACKFILL_CHECK_SECONDS
                return False
            done = embed_pending(self.db, self.embeddings, limit=BACKFILL_BATCH)
        except EmbeddingUnavailable as error:
            log.warning("embedding backlog: %s; retrying in %ss", error, BACKFILL_RETRY_SECONDS)
            self._backfill_after = now + BACKFILL_RETRY_SECONDS
            return False
        except Exception:  # noqa: BLE001 - e.g. the database restarting
            log.exception("embedding backlog failed")
            self._backfill_after = now + BACKFILL_RETRY_SECONDS
            return False
        if done.total:
            log.info("embedded %s pending clips, %s passages", done.clips, done.passages)
            self._themes_due = True
            return True
        self._backfill_after = now + BACKFILL_CHECK_SECONDS
        return False

    def _from_captions(self, run: dict, info: dict, settings: dict[str, str]) -> int | None:
        """Use YouTube's transcript when there is one; None means run Parakeet."""
        track = youtube.pick_caption_track(info, settings["youtube_captions"])
        if track is None:
            return None
        self.db.set_stage(run["id"], "captions", attempt=run["attempts"])
        try:
            words = transcript.caption_words(youtube.fetch_captions(track))
        except (youtube.YouTubeError, transcript.TranscriptError) as error:
            log.warning("run %s: captions unusable, transcribing instead: %s", run["id"], error)
            return None
        duration = info.get("duration")
        if duration and duration > 120 and words[-1]["end_seconds"] < duration * MIN_CAPTION_COVERAGE:
            log.warning("run %s: captions cover only part of the video, transcribing instead", run["id"])
            return None
        return self.db.save_transcript(
            run["episode_id"], ingest.word_rows(words), source=track.source, model=None,
            language=track.language, duration_ms=ingest.ms(duration) if duration else None,
        )

    def _transcribe(self, run: dict, settings: dict[str, str], client: httpx.Client, workdir: Path) -> int:
        """Download the audio and run Parakeet. A complete download is kept until its transcript is saved
        (or the run is cancelled, or AUDIO_KEEP_SECONDS pass), so a retry after Parakeet failed reuses it."""
        run_id, attempt = run["id"], run["attempts"]
        audio = self.audio_dir / f"{run['video_id']}.mp3"
        try:
            if audio.is_file():
                log.info("run %s: reusing the audio an earlier attempt downloaded", run_id)
            else:
                self.db.set_stage(run_id, "downloading", attempt=attempt)
                last = [0.0]

                def on_download(fraction: float) -> None:
                    if time.monotonic() - last[0] >= 0.5:
                        last[0] = time.monotonic()
                        self.db.update_run(run_id, attempt=attempt, progress=fraction)

                downloaded = youtube.download_audio(run["video_id"], workdir, on_download,
                                                    check=lambda: self.db.ensure_processing(run_id, attempt=attempt))
                audio.parent.mkdir(parents=True, exist_ok=True)
                os.replace(downloaded, audio)  # only ever a whole file: a retry can trust it
                audio.touch()  # kept for AUDIO_KEEP_SECONDS from now, whatever time the download carried

            self.db.set_stage(run_id, "transcribing", attempt=attempt)
            verbose = services.transcribe(client, settings["parakeet_url"], settings["parakeet_model"], audio)
            duration = verbose.get("duration")
            version_id = self.db.save_transcript(
                run["episode_id"], ingest.word_rows(transcript.timed_words(verbose)), source="parakeet",
                model=settings["parakeet_model"], language=verbose.get("language"),
                duration_ms=ingest.ms(duration) if duration else None,
            )
        except RunCancelled:
            audio.unlink(missing_ok=True)
            raise
        audio.unlink(missing_ok=True)
        return version_id

    def _sweep(self) -> None:
        """Delete kept audio no retry came for, and work folders a crashed attempt left behind. At start, at
        each run, and every SWEEP_SECONDS while idle."""
        self._swept_at = time.monotonic()
        cutoff = time.time() - AUDIO_KEEP_SECONDS
        for path in self.audio_dir.glob("*.mp3"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass  # e.g. already gone
        for path in (self.data_dir / "work").glob("*-*"):
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
