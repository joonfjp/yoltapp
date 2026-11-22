"""Channel subscriptions: check each subscribed channel for new long videos, as RipeClips does.

A check lists the channel's Videos tab (uploads) and/or its Live tab (finished livestreams), newest
first, and keeps the first three of each that are long enough (Settings, default 30 minutes), aren't
live or upcoming, and aren't members-only. RipeClips looks at the newest 5 uploads and 25 finished
streams; so does Yolt. Found videos wait on the Subscriptions page to be clipped, unless the subscription
has auto-clip on: then each check queues a run, with the default instructions, for every video found since
auto-clip was turned on that has no run yet (repo.autoclip_due). Those runs join the one queue like any other.

Checks run on their own thread, one channel at a time with a pause between them (more requests at
once make YouTube's bot check likelier), so a long first check never holds up a clipping run. Like
the run worker, only the Yolt server holding the database's worker lock checks.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from . import youtube
from .db import Database
from .repo import subscriptions as repo

log = logging.getLogger("yolt.subscriptions")

KEEP_PER_KIND = 3  # videos kept per channel and kind per check
LOOKBACK = {"upload": 5, "stream": 25}  # entries looked at per tab, newest first
SKIP_LIVE_STATUS = {"is_live", "is_upcoming", "post_live"}  # post_live: ended, still processing
SKIP_AVAILABILITY = {"subscriber_only", "premium_only", "needs_auth"}
POLL_SECONDS = 30.0  # how often an idle collector looks for due checks (a subscribe wakes it at once)
PAUSE_SECONDS = 0.5  # between channels


def tab_videos(info: dict, kind: str, *, min_seconds: int) -> list[dict]:
    """The videos a channel tab listing (youtube.fetch_channel) contributes, newest first."""
    out = []
    for entry in (info.get("entries") or [])[:LOOKBACK[kind]]:
        video_id = entry.get("id") or ""
        if not youtube.VIDEO_ID_RE.match(video_id):
            continue
        status = entry.get("live_status")
        if status in SKIP_LIVE_STATUS or (kind == "stream" and status != "was_live"):
            continue
        if entry.get("availability") in SKIP_AVAILABILITY:
            continue
        duration = entry.get("duration")
        if not duration or duration < max(min_seconds, 1):
            continue
        timestamp = entry.get("timestamp")
        out.append({
            "video_id": video_id,
            "title": entry.get("title") or f"YouTube video {video_id}",
            "duration_ms": round(duration * 1000),
            "published_at": datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None,
        })
        if len(out) == KEEP_PER_KIND:
            break
    return out


def schedule(settings: dict[str, str]) -> tuple[int, int]:
    """(hours between checks, shortest video kept in seconds) from Settings."""
    def whole(key: str) -> int:
        value = settings[key].strip()
        return int(value) if value.isascii() and value.isdigit() else 0
    return whole("subscription_check_hours"), whole("subscription_min_minutes") * 60


class Collector:
    def __init__(self, db: Database, *, poll_seconds: float = POLL_SECONDS, pause_seconds: float = PAUSE_SECONDS,
                 active: Callable[[], bool] = lambda: True, queued: Callable[[], None] = lambda: None):
        self.db = db
        self.active = active  # False while this server lost the worker lock to another one: leave checks to it
        self.queued = queued  # auto-clip queued runs: wake the run worker
        self.poll_seconds = poll_seconds
        self.pause_seconds = pause_seconds
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="yolt-subscriptions", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout)

    def wake(self) -> None:
        """A check was asked for, or the schedule changed: look now rather than at the next poll."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                checked = self.active() and self.check_next()
            except Exception:  # noqa: BLE001 - e.g. the database restarting; the thread must survive it
                log.exception("subscription check failed; trying again shortly")
                checked = False
            if checked:
                self._stopping.wait(self.pause_seconds)
            else:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    def check_next(self) -> bool:
        """Check the next due subscription. False if none was due."""
        hours, min_seconds = schedule(self.db.settings())
        with self.db.connect() as conn:
            due = repo.next_due(conn, hours)
        if due is None:
            return False
        self.check(due, min_seconds)
        return True

    def check(self, due: dict, min_seconds: int) -> None:
        path = f"/channel/{due['channel_id']}"
        errors, added = [], 0
        try:
            for kind, follows in (("upload", due["uploads"]), ("stream", due["streams"])):
                if not follows:
                    continue
                if self._stopping.is_set():  # a partial check isn't a check: it stays due for the next start
                    log.info("subscription %s (%s): check interrupted by shutdown", due["id"], due["name"])
                    return
                try:
                    info = youtube.fetch_channel(path, youtube.CHANNEL_TABS[kind], limit=LOOKBACK[kind])
                except youtube.NoSuchTab:
                    continue  # e.g. a channel that has never streamed: nothing to find, not a failure
                except youtube.YouTubeError as error:
                    errors.append(str(error))
                    continue
                videos = tab_videos(info, kind, min_seconds=min_seconds)
                with self.db.connect() as conn:
                    try:
                        repo.refresh_channel(conn, due["id"], due["podcast_id"], youtube.channel_details(info))
                    except youtube.YouTubeError:
                        pass  # the listing still says which videos are new
                    added += repo.save_videos(conn, due["podcast_id"], kind, videos)
            # Before the check is recorded, so a finished check has queued its runs. Even after a tab failed:
            # the other tab's videos, or ones an earlier check left, still are.
            if due["autoclip"]:
                self.autoclip(due)
        except Exception as unexpected:
            # Anything else (a yt-dlp bug, the database) fails the check too, so the retry backoff applies;
            # unrecorded, this subscription would stay first in line and be retried at every poll.
            self._failed(due, f"The check failed unexpectedly ({type(unexpected).__name__}); see the server log.")
            raise
        error = " ".join(dict.fromkeys(errors)) or None
        with self.db.connect() as conn:
            repo.finish_check(conn, due["id"], due["started_at"], error)
        if error:
            log.warning("subscription %s (%s): check failed: %s", due["id"], due["name"], error)
        else:
            log.info("subscription %s (%s): %s new video(s)", due["id"], due["name"], added)

    def autoclip(self, due: dict) -> int:
        """Queue a run for each video an auto-clip subscription has that needs one. Returns how many."""
        with self.db.connect() as conn:
            videos = repo.autoclip_due(conn, due["id"])
        if not videos:
            return 0
        instructions = self.db.settings()["clip_instructions"].strip()  # as a run with no instructions given
        for video in videos:
            self.db.submit(video["video_id"], instructions, video["meta"])
            with self.db.connect() as conn:
                repo.mark_autoclipped(conn, video["video_id"])
        log.info("subscription %s (%s): auto-clipping %s video(s)", due["id"], due["name"], len(videos))
        self.queued()
        return len(videos)

    def _failed(self, due: dict, error: str) -> None:
        try:
            with self.db.connect() as conn:
                repo.finish_check(conn, due["id"], due["started_at"], error)
        except Exception:  # noqa: BLE001 - e.g. the database is what failed; the caller logs the original error
            log.exception("subscription %s: couldn't record the failed check", due["id"])
