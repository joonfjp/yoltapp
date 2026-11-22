"""Speakers: people featured in episodes, with how much of the catalogue they're in."""

from __future__ import annotations

import psycopg

from .common import keyset_page

SPEAKERS_SQL = """
SELECT s.id AS _id, s.id, s.name,
       count(DISTINCT es.episode_id) AS episode_count,
       count(DISTINCT es.episode_id) FILTER (WHERE es.verified) AS verified_count
FROM speakers s JOIN episode_speakers es ON es.speaker_id = s.id
WHERE (%(q)s::text IS NULL OR s.name ILIKE '%%' || %(q)s || '%%')
GROUP BY s.id"""

# The clips of each speaker's episodes, counted for one page of speakers rather than all of them.
CLIP_COUNTS_SQL = """
SELECT es.speaker_id, count(*) AS clip_count
FROM episode_speakers es JOIN runs r ON r.episode_id = es.episode_id JOIN clips c ON c.run_id = r.id
WHERE es.speaker_id = ANY(%s)
GROUP BY es.speaker_id"""


def list_speakers(conn: psycopg.Connection, *, q: str | None, limit: int,
                  cursor: str | None) -> tuple[list[dict], str | None]:
    """Speakers of at least one episode, those in the most episodes first."""
    params = {"q": q.replace("%", r"\%").replace("_", r"\_") if q else None}
    sql = f"SELECT q0.*, q0.episode_count::double precision AS _k FROM ({SPEAKERS_SQL}) q0"
    page = keyset_page(conn, sql, params, descending=True, limit=limit, cursor=cursor, filters={"q": q})
    ids = [r["id"] for r in page.rows]
    clips = {r["speaker_id"]: r["clip_count"] for r in conn.execute(CLIP_COUNTS_SQL, (ids,))} if ids else {}
    return [{**{k: r[k] for k in ("id", "name", "episode_count", "verified_count")},
             "clip_count": clips.get(r["id"], 0)} for r in page.rows], page.next_cursor
