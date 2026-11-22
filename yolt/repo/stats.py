"""The home page's numbers (GET /stats): what the latest round of subscription checks found, and what's been
clipped lately.

Checks run in rounds: when channels fall due, the collector checks them one after another, a few seconds
each. A gap of more than ROUND_GAP between one check and the next starts a new round, so the latest round
is the latest check and every check before it with no such gap, however many channels it took. A video is
found during its channel's check, a little before that check's time is recorded, so the round's videos are
the ones found since SLACK before its first check (SLACK < ROUND_GAP keeps the round before out).
"""

from __future__ import annotations

import psycopg

from .common import iso

ROUND_GAP = "10 minutes"
SLACK = "5 minutes"
CLIPPED_DAYS = 7

SQL = f"""
WITH checks AS (
    SELECT checked_at, lag(checked_at) OVER (ORDER BY checked_at DESC) AS next_check
    FROM subscriptions WHERE checked_at IS NOT NULL
), latest AS (
    SELECT max(checked_at) AS last_at,
           COALESCE((SELECT next_check FROM checks WHERE next_check - checked_at > interval '{ROUND_GAP}'
                     ORDER BY checked_at DESC LIMIT 1), min(checked_at)) AS started_at
    FROM checks
)
SELECT latest.last_at,
       (SELECT count(*) FROM channel_videos v
        WHERE v.discovered_at >= latest.started_at - interval '{SLACK}') AS new_videos,
       (SELECT count(*) FROM clips c JOIN runs r ON r.id = c.run_id
        WHERE r.status = 'ready' AND r.finished_at > now() - make_interval(days => %(days)s)) AS clips,
       (SELECT count(DISTINCT episode_id) FROM runs
        WHERE status = 'ready' AND finished_at > now() - make_interval(days => %(days)s)) AS episodes
FROM latest"""


def home(conn: psycopg.Connection) -> dict:
    row = conn.execute(SQL, {"days": CLIPPED_DAYS}).fetchone()
    return {
        "subscriptions": {"last_checked_at": iso(row["last_at"]), "new_videos": row["new_videos"]},
        "clipped": {"days": CLIPPED_DAYS, "clips": row["clips"], "episodes": row["episodes"]},
    }
