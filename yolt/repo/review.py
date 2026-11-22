"""Gather what the editorial review needs for saved or draft items: their words, the words before, vectors.

Yolt keeps every timed word, so an item's text and what precedes it come straight from the word table.
"""

from __future__ import annotations

import json

import psycopg

from ..domain import review as checks
from ..domain.segments import Resolved
from .common import speakers_by_episode
from .compilations import revision_row

CONTEXT_WORDS = 12

# An item's vector: the mean of the passages inside its word range (so trims count), else its clip's.
ITEM_VECTORS_SQL = """
SELECT x.n, COALESCE(
    (SELECT avg(pv.embedding) FROM passages p
       JOIN passage_vectors pv ON pv.passage_id = p.id AND pv.model_name = %(model)s AND vector_norm(pv.embedding) > 0
      WHERE p.clip_id = x.clip AND p.last_word_seq >= x.f AND p.first_word_seq <= x.l),
    (SELECT embedding FROM clip_vectors WHERE clip_id = x.clip AND model_name = %(model)s
        AND vector_norm(embedding) > 0))::text AS v
FROM unnest(%(n)s::int[], %(clip)s::bigint[], %(f)s::int[], %(l)s::int[]) AS x(n, clip, f, l)"""

# Each item's own words, and the CONTEXT_WORDS before it in its transcript.
WORDS_SQL = """
SELECT x.n,
       (SELECT string_agg(w.word, ' ' ORDER BY w.seq) FROM transcript_words w
         WHERE w.transcript_version_id = x.tv AND w.seq BETWEEN x.f AND x.l) AS spoken,
       (SELECT string_agg(w.word, ' ' ORDER BY w.seq) FROM transcript_words w
         WHERE w.transcript_version_id = x.tv AND w.seq BETWEEN x.f - %(context)s AND x.f - 1) AS before
FROM unnest(%(n)s::int[], %(tv)s::bigint[], %(f)s::int[], %(l)s::int[]) AS x(n, tv, f, l)"""


def review_revision(conn: psycopg.Connection, revision_id: int, model: str, target_duration_ms: int | None) -> dict:
    revision_row(conn, revision_id)  # 404 if missing
    rows = conn.execute(
        """SELECT "position", clip_id, transcript_version_id AS transcript_id, first_word_seq AS first,
                  last_word_seq AS last, start_ms, end_ms
           FROM compilation_items WHERE compilation_id = %s ORDER BY "position\"""", (revision_id,)).fetchall()
    return _review(conn, [dict(r) for r in rows], model, target_duration_ms)


def review_segments(conn: psycopg.Connection, resolved: list[Resolved], model: str,
                    target_duration_ms: int | None) -> dict:
    rows = [{"position": n, "clip_id": s.clip_id, "transcript_id": s.transcript_id, "first": s.first,
             "last": s.last, "start_ms": s.start_ms, "end_ms": s.end_ms} for n, s in enumerate(resolved, 1)]
    return _review(conn, rows, model, target_duration_ms)


def _review(conn: psycopg.Connection, rows: list[dict], model: str, target_duration_ms: int | None) -> dict:
    has_vectors = conn.execute("SELECT 1 FROM clip_vectors WHERE model_name = %s LIMIT 1", (model,)).fetchone() is not None
    if not rows:
        return checks.review([], target_duration_ms=target_duration_ms, vectors=has_vectors)
    punctuated = {r["id"]: r["punctuated"] for r in conn.execute(
        "SELECT id, punctuated FROM transcript_versions WHERE id = ANY(%s)",
        (list({r["transcript_id"] for r in rows}),))}
    episodes = {r["id"]: r["episode_id"] for r in conn.execute(
        "SELECT c.id, r.episode_id FROM clips c JOIN runs r ON r.id = c.run_id WHERE c.id = ANY(%s)",
        (list({r["clip_id"] for r in rows}),))}
    positions = [r["position"] for r in rows]
    words = {r["n"]: r for r in conn.execute(WORDS_SQL, {
        "context": CONTEXT_WORDS, "n": positions, "tv": [r["transcript_id"] for r in rows],
        "f": [r["first"] for r in rows], "l": [r["last"] for r in rows]})}
    item_vectors: dict[int, list[float] | None] = {}
    if has_vectors:
        item_vectors = {r["n"]: json.loads(r["v"]) if r["v"] else None for r in conn.execute(ITEM_VECTORS_SQL, {
            "model": model, "n": positions, "clip": [r["clip_id"] for r in rows],
            "f": [r["first"] for r in rows], "l": [r["last"] for r in rows]})}
    speakers = speakers_by_episode(conn, list(set(episodes.values())))
    items = []
    for r in rows:
        spoken, before = words[r["position"]]["spoken"] or "", words[r["position"]]["before"] or ""
        items.append(checks.Item(
            position=r["position"], clip_id=r["clip_id"], episode_id=episodes[r["clip_id"]],
            duration_ms=r["end_ms"] - r["start_ms"], text=spoken, before=before, alignment="exact",
            speakers=[s["name"] for s in speakers.get(episodes[r["clip_id"]], [])],
            vector=item_vectors.get(r["position"]), transcript_id=r["transcript_id"],
            words=(r["first"], r["last"]), time=(r["start_ms"], r["end_ms"]), spoken=spoken,
            punctuated=punctuated[r["transcript_id"]]))
    return checks.review(items, target_duration_ms=target_duration_ms, vectors=has_vectors)
