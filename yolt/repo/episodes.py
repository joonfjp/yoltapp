"""Episodes (videos) and their transcripts."""

from __future__ import annotations

import psycopg

from .. import ingest, transcript
from ..embed import EMBEDDED_CLIP
from ..problems import not_found
from .common import Page, episode_info, iso, keyset_page, speakers_by_episode

SNIPPET_SEP = "␞"  # never occurs in transcripts; splits ts_headline fragments

TRANSCRIPT_COLUMNS = """tv.id, tv.episode_id, tv.source, tv.model, tv.language, tv.word_count, tv.duration_ms,
    tv.punctuated, char_length(tv.full_text) AS text_chars, octet_length(tv.full_text) AS text_bytes, tv.created_at"""


def transcript_json(row: dict) -> dict:
    return {**{k: row[k] for k in ("id", "episode_id", "source", "model", "language", "word_count", "duration_ms",
                                   "punctuated", "text_chars", "text_bytes")},
            "created_at": iso(row["created_at"])}


def list_episodes(conn: psycopg.Connection, *, q: str | None, podcast_ids: list[int], speaker_ids: list[int],
                  published_after: str | None, limit: int, cursor: str | None,
                  model: str) -> tuple[list[dict], str | None]:
    params: dict = {"q": q, "podcasts": podcast_ids, "speakers": speaker_ids, "after": published_after}
    where = ["TRUE"]
    if speaker_ids:
        where.append("EXISTS (SELECT 1 FROM episode_speakers es WHERE es.episode_id = e.id "
                     "AND es.speaker_id = ANY(%(speakers)s))")
    if podcast_ids:
        where.append("e.podcast_id = ANY(%(podcasts)s)")
    if published_after:
        where.append("e.published_at >= %(after)s::date")
    if q:  # each episode's best-matching transcript; the stored, GIN-indexed `fts` finds and ranks them
        sql = f"""
            SELECT e.id AS _id, m.relevance AS _k, m.transcript_id, m.relevance
            FROM (
                SELECT DISTINCT ON (tv.episode_id) tv.episode_id, tv.id AS transcript_id,
                       ts_rank_cd(tv.fts, query) AS relevance
                FROM transcript_versions tv, websearch_to_tsquery('english', %(q)s) query
                WHERE tv.fts @@ query
                ORDER BY tv.episode_id, relevance DESC, tv.id DESC
            ) m
            JOIN episodes e ON e.id = m.episode_id
            WHERE {' AND '.join(where)}"""
    else:
        sql = f"""SELECT e.id AS _id, COALESCE(extract(epoch FROM e.published_at), 0)::double precision AS _k
                  FROM episodes e WHERE {' AND '.join(where)}"""
    page: Page = keyset_page(conn, sql, params, descending=True, limit=limit, cursor=cursor, filters=dict(params))
    episodes = load_episodes(conn, [r["_id"] for r in page.rows], model)
    if q:
        snippets = _snippets(conn, q, [r["transcript_id"] for r in page.rows])
        for ep, r in zip(episodes, page.rows, strict=True):
            ep["match"] = {"transcript_id": r["transcript_id"], "relevance": float(r["relevance"]),
                           "snippets": snippets.get(r["transcript_id"], [])}
    return episodes, page.next_cursor


def _snippets(conn: psycopg.Connection, q: str, transcript_ids: list[int]) -> dict[int, list[str]]:
    rows = conn.execute(
        f"""SELECT tv.id, ts_headline('english', tv.full_text, websearch_to_tsquery('english', %s),
                   'StartSel=«, StopSel=», MaxFragments=5, MaxWords=18, MinWords=8, FragmentDelimiter={SNIPPET_SEP}')
            FROM transcript_versions tv WHERE tv.id = ANY(%s)""", (q, transcript_ids)).fetchall()
    return {r["id"]: [s.strip() for s in r["ts_headline"].split(SNIPPET_SEP) if s.strip()][:5] for r in rows}


def load_episodes(conn: psycopg.Connection, ids: list[int], model: str) -> list[dict]:
    """Full Episode objects, in the order of `ids`. `model` is the embedding model clips are counted against."""
    if not ids:
        return []
    rows = {r["episode_id"]: r for r in conn.execute(
        f"""SELECT e.id AS episode_id, e.title AS episode_title, e.published_at, e.platform, e.video_id,
                  e.source_url, e.duration_ms, e.podcast_id, p.name AS podcast, p.url AS podcast_url,
                  s.id AS subscription_id, s.uploads AS subscription_uploads, s.streams AS subscription_streams,
                  (SELECT count(*) FROM clips c JOIN runs r ON r.id = c.run_id WHERE r.episode_id = e.id) AS clip_count,
                  (SELECT count(*) FROM clips c JOIN runs r ON r.id = c.run_id
                    WHERE r.episode_id = e.id AND {EMBEDDED_CLIP}) AS embedded_clip_count,
                  (SELECT count(*) FROM runs r WHERE r.episode_id = e.id) AS run_count
           FROM episodes e LEFT JOIN podcasts p ON p.id = e.podcast_id
           LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
           WHERE e.id = ANY(%(ids)s)""",
        {"ids": ids, "m": model})}
    transcripts: dict[int, list[dict]] = {i: [] for i in ids}
    for t in conn.execute(
        f"""SELECT {TRANSCRIPT_COLUMNS}, max(r.created_at) AS latest_run
            FROM transcript_versions tv JOIN runs r ON r.transcript_version_id = tv.id
            WHERE tv.episode_id = ANY(%s) GROUP BY tv.id ORDER BY latest_run DESC, tv.id DESC""", (ids,)):
        transcripts[t["episode_id"]].append(transcript_json(t))
    speakers = speakers_by_episode(conn, ids)
    return [{**episode_info(rows[i]), "podcast_id": rows[i]["podcast_id"], "subscription": _subscription(rows[i]),
             "speakers": speakers[i], "transcripts": transcripts[i],
             "clip_count": rows[i]["clip_count"], "embedded_clip_count": rows[i]["embedded_clip_count"],
             "run_count": rows[i]["run_count"]} for i in ids if i in rows]


def _subscription(row: dict) -> dict | None:
    """The episode's channel subscription, if there is one."""
    if row["subscription_id"] is None:
        return None
    return {"id": row["subscription_id"], "uploads": row["subscription_uploads"],
            "streams": row["subscription_streams"]}


def get_episode(conn: psycopg.Connection, episode_id: int, model: str) -> dict:
    found = load_episodes(conn, [episode_id], model)
    if not found:
        raise not_found("Episode")
    return found[0]


def get_transcript(conn: psycopg.Connection, transcript_id: int) -> dict:
    row = conn.execute(f"SELECT {TRANSCRIPT_COLUMNS} FROM transcript_versions tv WHERE tv.id = %s",
                       (transcript_id,)).fetchone()
    if row is None:
        raise not_found("Transcript")
    return transcript_json(row)


def transcript_text(conn: psycopg.Connection, transcript_id: int) -> str:
    row = conn.execute("SELECT full_text FROM transcript_versions WHERE id = %s", (transcript_id,)).fetchone()
    if row is None:
        raise not_found("Transcript")
    return row["full_text"]


def words(conn: psycopg.Connection, transcript_id: int, first: int, last: int) -> list[dict]:
    return conn.execute(
        """SELECT seq, word, start_ms, end_ms FROM transcript_words
           WHERE transcript_version_id = %s AND seq BETWEEN %s AND %s ORDER BY seq""",
        (transcript_id, first, last)).fetchall()


def transcript_srt(conn: psycopg.Connection, transcript_id: int) -> str:
    """Caption rows built from the words, the same SRT Highlightr was given."""
    get_transcript(conn, transcript_id)
    rows = conn.execute(
        "SELECT seq, word, start_ms, end_ms FROM transcript_words WHERE transcript_version_id = %s ORDER BY seq",
        (transcript_id,)).fetchall()
    return transcript.rows_to_srt(transcript.make_rows(ingest.as_seconds([ingest.Word(**r) for r in rows])))
