"""Subscriptions: channels Yolt checks for new uploads and finished livestreams, and the videos found.

A subscription is to a podcast row (Yolt's word for a YouTube channel). The videos a check finds live
in channel_videos, keyed by video, and outlast an unsubscribe. A found video counts as clipped once it
has a run that's queued, processing or ready.

A subscription with auto-clip on (autoclip_since: when it was turned on) has each check queue a run for
the videos found since then that have no run yet, once each (channel_videos.autoclipped_at).
"""

from __future__ import annotations

from datetime import datetime

import psycopg

from .. import youtube
from ..db import PLATFORM
from ..problems import ApiProblem, FieldError, invalid, not_found
from .common import iso, keyset_page

RETRY_MINUTES = (1, 5)  # after a failed scheduled check, as RipeClips retries its collection

# Whether a subscription's check is due, and when the next scheduled one is, given %(hours)s
# between checks (0: only when asked).
DUE_SQL = f"""(s.check_requested_at IS NOT NULL OR (%(hours)s > 0 AND (
    s.checked_at IS NULL
    OR s.checked_at < now() - make_interval(hours => %(hours)s)
    OR (s.check_failures = 1 AND s.checked_at < now() - interval '{RETRY_MINUTES[0]} minutes')
    OR (s.check_failures = 2 AND s.checked_at < now() - interval '{RETRY_MINUTES[1]} minutes'))))"""
NEXT_SQL = f"""CASE WHEN s.check_requested_at IS NOT NULL THEN s.check_requested_at
    WHEN %(hours)s = 0 THEN NULL
    WHEN s.checked_at IS NULL THEN s.created_at
    WHEN s.check_failures = 1 THEN s.checked_at + interval '{RETRY_MINUTES[0]} minutes'
    WHEN s.check_failures = 2 THEN s.checked_at + interval '{RETRY_MINUTES[1]} minutes'
    ELSE s.checked_at + make_interval(hours => %(hours)s) END"""

CLIPPED_SQL = """EXISTS (SELECT 1 FROM episodes e JOIN runs r ON r.episode_id = e.id
    WHERE e.platform = v.platform AND e.video_id = v.video_id AND r.status IN ('queued', 'processing', 'ready'))"""

SUBSCRIPTION_SQL = f"""
SELECT s.id, s.podcast_id, p.external_id AS channel_id, p.name, p.url, s.handle, s.uploads, s.streams,
       s.autoclip_since, s.created_at, s.checked_at, s.check_error, {DUE_SQL} AS check_pending, {NEXT_SQL} AS next_check_at,
       (SELECT count(*) FROM channel_videos v WHERE v.podcast_id = s.podcast_id) AS video_count,
       (SELECT count(*) FROM channel_videos v WHERE v.podcast_id = s.podcast_id AND NOT {CLIPPED_SQL})
         AS unclipped_count
FROM subscriptions s JOIN podcasts p ON p.id = s.podcast_id"""

# Found videos newest published first, keyed for keyset_page; VIDEO_SQL loads each page's rows.
VIDEO_KEY_SQL = """
SELECT v.id AS _id, extract(epoch FROM coalesce(v.published_at, v.discovered_at))::double precision AS _k
FROM channel_videos v"""

# The run a found video links to: one that's still going, else a ready one, else the latest.
VIDEO_SQL = """
SELECT v.id, v.video_id, v.kind, v.title, v.duration_ms, v.published_at, v.discovered_at,
       v.podcast_id, p.name AS channel_name, p.url AS channel_url, s.id AS subscription_id,
       e.id AS episode_id, lr.id AS run_id, lr.status AS run_status, lr.clip_count AS run_clip_count
FROM channel_videos v
JOIN podcasts p ON p.id = v.podcast_id
LEFT JOIN subscriptions s ON s.podcast_id = v.podcast_id
LEFT JOIN episodes e ON e.platform = v.platform AND e.video_id = v.video_id
LEFT JOIN LATERAL (
    SELECT r.id, r.status, (SELECT count(*) FROM clips c WHERE c.run_id = r.id) AS clip_count
    FROM runs r WHERE r.episode_id = e.id
    ORDER BY r.status IN ('queued', 'processing') DESC, r.status = 'ready' DESC, r.created_at DESC, r.id DESC
    LIMIT 1) lr ON TRUE"""


def subscription_json(r: dict) -> dict:
    return {
        "id": r["id"], "podcast_id": r["podcast_id"], "channel_id": r["channel_id"], "name": r["name"],
        "url": r["url"], "handle": r["handle"], "uploads": r["uploads"], "streams": r["streams"],
        "autoclip": r["autoclip_since"] is not None, "autoclip_since": iso(r["autoclip_since"]),
        "created_at": iso(r["created_at"]), "checked_at": iso(r["checked_at"]), "check_error": r["check_error"],
        "check_pending": r["check_pending"], "next_check_at": iso(r["next_check_at"]),
        "video_count": r["video_count"], "unclipped_count": r["unclipped_count"],
    }


def video_json(r: dict) -> dict:
    run = None
    if r["run_id"] is not None:
        run = {"id": r["run_id"], "status": r["run_status"], "clip_count": r["run_clip_count"]}
    return {
        "id": r["id"], "video_id": r["video_id"], "url": youtube.watch_url(r["video_id"]), "title": r["title"],
        "kind": r["kind"], "duration_ms": r["duration_ms"], "published_at": iso(r["published_at"]),
        "discovered_at": iso(r["discovered_at"]), "thumbnail_url": youtube.small_thumbnail_url(r["video_id"]),
        "channel": {"podcast_id": r["podcast_id"], "name": r["channel_name"], "url": r["channel_url"],
                    "subscription_id": r["subscription_id"]},
        "episode_id": r["episode_id"], "run": run,
    }


# --- reads ------------------------------------------------------------------------

def list_subscriptions(conn: psycopg.Connection, *, hours: int, limit: int,
                       cursor: str | None) -> tuple[list[dict], str | None]:
    """By channel name. The cursor holds the last name itself, so pages stay put as subscriptions come and go."""
    sql = f"SELECT q0.*, q0.id AS _id, lower(q0.name) AS _k FROM ({SUBSCRIPTION_SQL}) q0"
    page = keyset_page(conn, sql, {"hours": hours}, descending=False, limit=limit, cursor=cursor, filters={},
                       text_key=True)
    return [subscription_json(r) for r in page.rows], page.next_cursor


def get_subscription(conn: psycopg.Connection, subscription_id: int, *, hours: int) -> dict:
    row = conn.execute(f"{SUBSCRIPTION_SQL} WHERE s.id = %(id)s", {"id": subscription_id, "hours": hours}).fetchone()
    if row is None:
        raise not_found("Subscription")
    return subscription_json(row)


def for_channel(conn: psycopg.Connection, channel_id: str | None) -> dict | None:
    """The subscription to a YouTube channel (by its UC… id), as an episode shows it, if there is one."""
    if not channel_id:
        return None
    return conn.execute(
        """SELECT s.id, s.uploads, s.streams FROM subscriptions s JOIN podcasts p ON p.id = s.podcast_id
           WHERE p.platform = %s AND p.external_id = %s""", (PLATFORM, channel_id)).fetchone()


def list_videos(conn: psycopg.Connection, *, kind: str | None, podcast_ids: list[int], clipped: bool | None,
                limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
    """Newest published first."""
    where = ["TRUE"]
    params: dict = {"kind": kind, "podcasts": podcast_ids}
    if kind:
        where.append("v.kind = %(kind)s")
    if podcast_ids:
        where.append("v.podcast_id = ANY(%(podcasts)s)")
    if clipped is not None:
        where.append(CLIPPED_SQL if clipped else f"NOT {CLIPPED_SQL}")
    sql = f"{VIDEO_KEY_SQL} WHERE {' AND '.join(where)}"
    filters = {"kind": kind, "podcast_id": sorted(podcast_ids), "clipped": clipped}
    page = keyset_page(conn, sql, params, descending=True, limit=limit, cursor=cursor, filters=filters)
    ids = [r["_id"] for r in page.rows]  # the run lookups, for this page only
    rows = {r["id"]: r for r in conn.execute(f"{VIDEO_SQL} WHERE v.id = ANY(%s)", (ids,))} if ids else {}
    return [video_json(rows[i]) for i in ids if i in rows], page.next_cursor


# --- writes from the API ------------------------------------------------------------

def _follows_nothing() -> ApiProblem:
    return invalid([FieldError("nothing_followed", "Follow uploads, finished livestreams, or both.",
                               pointer="/uploads")])


def subscribe(conn: psycopg.Connection, channel: dict, *, uploads: bool, streams: bool,
              autoclip: bool | None = None) -> tuple[int, bool]:
    """Subscribe to a channel (youtube.channel_details) or, if already subscribed, set what it follows.
    `autoclip` None leaves a subscription as it is (a new one off). Returns (subscription_id, created). A
    new subscription, or a newly followed kind, is checked next."""
    if not (uploads or streams):
        raise _follows_nothing()
    podcast_id = conn.execute(
        "INSERT INTO podcasts (platform, external_id, name, url) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (platform, external_id) DO UPDATE SET name = excluded.name, url = excluded.url RETURNING id",
        (PLATFORM, channel["channel_id"], channel["name"], channel["url"]),
    ).fetchone()["id"]
    row = conn.execute(
        "INSERT INTO subscriptions (podcast_id, handle, uploads, streams, autoclip_since) "
        "VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() END) ON CONFLICT (podcast_id) DO NOTHING RETURNING id",
        (podcast_id, channel["handle"], uploads, streams, bool(autoclip)),
    ).fetchone()
    if row:
        return row["id"], True
    existing = conn.execute("SELECT id FROM subscriptions WHERE podcast_id = %s", (podcast_id,)).fetchone()["id"]
    conn.execute("UPDATE subscriptions SET handle = coalesce(%s, handle) WHERE id = %s", (channel["handle"], existing))
    update(conn, existing, uploads=uploads, streams=streams, autoclip=autoclip)
    return existing, False


def update(conn: psycopg.Connection, subscription_id: int, *, uploads: bool | None, streams: bool | None,
           autoclip: bool | None = None) -> None:
    """None leaves a setting as it is. Turning auto-clip on applies to videos found from now on."""
    current = conn.execute("SELECT uploads, streams, autoclip_since IS NOT NULL AS autoclip FROM subscriptions "
                           "WHERE id = %s FOR UPDATE", (subscription_id,)).fetchone()
    if current is None:
        raise not_found("Subscription")
    new_uploads = current["uploads"] if uploads is None else uploads
    new_streams = current["streams"] if streams is None else streams
    if not (new_uploads or new_streams):
        raise _follows_nothing()
    if autoclip is not None and autoclip != current["autoclip"]:
        conn.execute("UPDATE subscriptions SET autoclip_since = CASE WHEN %s THEN now() END, updated_at = now() "
                     "WHERE id = %s", (autoclip, subscription_id))
    if (new_uploads, new_streams) == (current["uploads"], current["streams"]):
        return
    newly_followed = (new_uploads and not current["uploads"]) or (new_streams and not current["streams"])
    conn.execute(
        "UPDATE subscriptions SET uploads = %s, streams = %s, updated_at = now(), "
        "check_requested_at = CASE WHEN %s THEN now() ELSE check_requested_at END WHERE id = %s",
        (new_uploads, new_streams, newly_followed, subscription_id),
    )


def export_subscriptions(conn: psycopg.Connection) -> list[dict]:
    """Every subscription as an export file lists it (import_subscriptions reads it back), by channel name."""
    rows = conn.execute(
        """SELECT p.external_id AS channel_id, p.name, s.handle, p.url, s.uploads, s.streams,
                  s.autoclip_since IS NOT NULL AS autoclip
           FROM subscriptions s JOIN podcasts p ON p.id = s.podcast_id
           ORDER BY lower(p.name), s.id""").fetchall()
    return [dict(r) for r in rows]


def import_subscriptions(conn: psycopg.Connection, channels: list[dict]) -> tuple[int, int]:
    """Subscribe to each channel of an export file (channel_id, name, handle, uploads, streams, autoclip),
    without asking YouTube: the file says which channel it is, and its first check refreshes the name and
    link. A channel already followed is left as it is, and so is the name of one Yolt already knows. Returns
    (created, already followed)."""
    created = 0
    for c in channels:
        podcast_id = conn.execute(
            "INSERT INTO podcasts (platform, external_id, name, url) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (platform, external_id) DO UPDATE SET name = podcasts.name RETURNING id",
            (PLATFORM, c["channel_id"], c["name"], f"https://www.youtube.com/channel/{c['channel_id']}"),
        ).fetchone()["id"]
        created += conn.execute(
            "INSERT INTO subscriptions (podcast_id, handle, uploads, streams, autoclip_since) "
            "VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() END) ON CONFLICT (podcast_id) DO NOTHING",
            (podcast_id, c["handle"], c["uploads"], c["streams"], c["autoclip"]),
        ).rowcount
    return created, len(channels) - created


def unsubscribe(conn: psycopg.Connection, subscription_id: int) -> None:
    """The videos found so far stay."""
    if conn.execute("DELETE FROM subscriptions WHERE id = %s", (subscription_id,)).rowcount == 0:
        raise not_found("Subscription")


def request_checks(conn: psycopg.Connection) -> int:
    """Ask for every subscription to be checked now. Returns how many there are."""
    return conn.execute("UPDATE subscriptions SET check_requested_at = now()").rowcount


def request_check(conn: psycopg.Connection, subscription_id: int) -> None:
    """Ask for one subscription to be checked now (after any already asked for)."""
    if conn.execute("UPDATE subscriptions SET check_requested_at = now() WHERE id = %s",
                    (subscription_id,)).rowcount == 0:
        raise not_found("Subscription")


# --- the collector's side ------------------------------------------------------------

def next_due(conn: psycopg.Connection, hours: int) -> dict | None:
    """The subscription to check next (asked-for checks first, then the longest unchecked), with the
    database time the check starts at, or None if none is due."""
    return conn.execute(
        f"""SELECT s.id, s.podcast_id, s.uploads, s.streams, s.autoclip_since IS NOT NULL AS autoclip,
                   p.external_id AS channel_id, p.name, now() AS started_at
            FROM subscriptions s JOIN podcasts p ON p.id = s.podcast_id
            WHERE {DUE_SQL}
            ORDER BY s.check_requested_at IS NULL, s.check_requested_at, s.checked_at NULLS FIRST, s.id
            LIMIT 1""",
        {"hours": hours},
    ).fetchone()


def save_videos(conn: psycopg.Connection, podcast_id: int, kind: str, videos: list[dict]) -> int:
    """Add or refresh found videos (subscriptions.tab_videos, newest first). Returns how many are new. A
    video keeps the publish time it was first seen with: "3 hours ago" then is more precise than "2 weeks
    ago" later. They're added oldest first, so videos the channel page dates alike ("2 weeks ago") still
    list newest first (ties go to the higher id)."""
    added = 0
    for v in reversed(videos):
        row = conn.execute(
            """INSERT INTO channel_videos (platform, video_id, podcast_id, kind, title, duration_ms, published_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (platform, video_id) DO UPDATE SET podcast_id = excluded.podcast_id,
                   kind = excluded.kind, title = excluded.title, duration_ms = excluded.duration_ms,
                   published_at = coalesce(channel_videos.published_at, excluded.published_at), updated_at = now()
               RETURNING (xmax = 0) AS inserted""",
            (PLATFORM, v["video_id"], podcast_id, kind, v["title"], v["duration_ms"], v["published_at"]),
        ).fetchone()
        added += bool(row["inserted"])
    return added


def refresh_channel(conn: psycopg.Connection, subscription_id: int, podcast_id: int, channel: dict) -> None:
    """What the channel's page now says it's called."""
    conn.execute("UPDATE podcasts SET name = %s, url = %s WHERE id = %s",
                 (channel["name"], channel["url"], podcast_id))
    if channel["handle"]:
        conn.execute("UPDATE subscriptions SET handle = %s WHERE id = %s", (channel["handle"], subscription_id))


def finish_check(conn: psycopg.Connection, subscription_id: int, started_at: datetime, error: str | None) -> None:
    """Record a check. A check asked for after this one started is still due."""
    conn.execute(
        """UPDATE subscriptions SET checked_at = now(), check_error = %(error)s,
               check_failures = CASE WHEN %(error)s::text IS NULL THEN 0 ELSE check_failures + 1 END,
               check_requested_at = CASE WHEN check_requested_at <= %(started)s THEN NULL
                                         ELSE check_requested_at END
           WHERE id = %(id)s""",
        {"error": error, "started": started_at, "id": subscription_id},
    )


def autoclip_due(conn: psycopg.Connection, subscription_id: int) -> list[dict]:
    """The videos an auto-clip subscription should queue a run for, oldest published first: found since
    auto-clip was turned on, not auto-clipped before, and without a run of any status (so a failed or
    cancelled one isn't retried by itself, and one clipped by hand isn't clipped again). Each comes with
    the episode details (db.submit's `meta`) the channel listing gave; the run fetches the exact ones."""
    rows = conn.execute(
        """SELECT v.video_id, v.title, v.duration_ms, v.published_at,
                  p.name AS channel, p.external_id AS channel_id, p.url AS channel_url
           FROM subscriptions s
           JOIN podcasts p ON p.id = s.podcast_id
           JOIN channel_videos v ON v.podcast_id = s.podcast_id
           WHERE s.id = %s AND s.autoclip_since IS NOT NULL AND v.discovered_at >= s.autoclip_since
             AND v.autoclipped_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM episodes e JOIN runs r ON r.episode_id = e.id
                             WHERE e.platform = v.platform AND e.video_id = v.video_id)
           ORDER BY v.published_at NULLS LAST, v.id""",
        (subscription_id,),
    ).fetchall()
    return [{"video_id": r["video_id"], "meta": {
        "title": r["title"], "channel": r["channel"], "channel_id": r["channel_id"], "channel_url": r["channel_url"],
        "published_at": r["published_at"], "duration_seconds": r["duration_ms"] / 1000 if r["duration_ms"] else None,
    }} for r in rows]


def mark_autoclipped(conn: psycopg.Connection, video_id: str) -> None:
    conn.execute("UPDATE channel_videos SET autoclipped_at = now() WHERE platform = %s AND video_id = %s",
                 (PLATFORM, video_id))
