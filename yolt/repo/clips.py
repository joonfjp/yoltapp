"""Clips: keyword and semantic search, facets, batch loads, share links, and the words around a clip."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg

from ..domain import context as ctx
from ..domain.diversity import cosine_scale, mmr
from ..domain.segments import ClipWords, Word
from ..problems import FieldError, bad_request, not_found
from . import vectors
from .common import CLIP_USAGE_SQL, LATEST_SQL, clip_links, episode_ref, iso, keyset_page, speakers_by_episode

SORTS: dict[str, tuple[str, bool]] = {  # name -> (sort-key SQL, descending)
    "-relevance": ("m.relevance", True),
    "-score": ("COALESCE(c.overall_score, -1e9)", True),
    "score": ("COALESCE(c.overall_score, -1e9)", False),
    "rank": ("c.rank", False),
    "-published_at": ("COALESCE(extract(epoch FROM e.published_at), 0)", True),
    "published_at": ("COALESCE(extract(epoch FROM e.published_at), 0)", False),
    # Newest run first, best-ranked first within it: one exact numeric key (ms * 1000 - rank).
    "-created": ("floor(extract(epoch FROM c.created_at) * 1000) * 1000 - c.rank", True),
    # rendered first, then compilations; packed into one numeric key.
    "-rendered": ("(COALESCE(u.rendered, 0) * 1000000 + COALESCE(u.compilations, 0))", True),
    "rendered": ("(COALESCE(u.rendered, 0) * 1000000 + COALESCE(u.compilations, 0))", False),
}

# For sort=rendered: usage computed once for every used clip, not per row.
USAGE_JOIN = f"LEFT JOIN ({CLIP_USAGE_SQL}) u ON u.clip_id = c.id"

USED = "EXISTS (SELECT 1 FROM compilation_items ci WHERE ci.clip_id = c.id)"
RENDERED = """EXISTS (SELECT 1 FROM compilation_items ci
    JOIN compilation_renders r ON r.revision_id = ci.compilation_id
    JOIN compilation_render_positions rp ON rp.render_id = r.id AND rp."position" = ci."position"
    WHERE ci.clip_id = c.id)"""

CLIP_FROM = """FROM clips c
JOIN runs r ON r.id = c.run_id
JOIN episodes e ON e.id = r.episode_id"""

MATCH_JOIN = f"JOIN ({vectors.KEYWORD_SQL}) m ON m.clip_id = c.id"
# Semantic modes rank by similarity (semantic, like) or by the fused rank (hybrid), joined as `m`.
RANKED_JOINS = {"semantic": f"JOIN ({vectors.SIMILARITY_SQL}) m ON m.clip_id = c.id",
                "like": f"JOIN ({vectors.SIMILARITY_SQL}) m ON m.clip_id = c.id",
                "hybrid": f"JOIN ({vectors.HYBRID_SQL}) m ON m.clip_id = c.id"}
RANKED_KEYS = {"semantic": "m.similarity", "like": "m.similarity", "hybrid": "m.fused"}

INCLUDES = {"quotes", "speakers", "details"}


@dataclass(frozen=True)
class ClipFilters:
    """Everything that selects clips, shared by GET /clips and GET /clips/facets so their counts agree."""
    ids: list[int] = field(default_factory=list)
    q: str | None = None
    episode_ids: list[int] = field(default_factory=list)
    transcript_ids: list[int] = field(default_factory=list)
    run_ids: list[int] = field(default_factory=list)
    speaker_ids: list[int] = field(default_factory=list)
    min_score: float | None = None
    min_duration_ms: int | None = None
    max_duration_ms: int | None = None
    exclude_ids: list[int] = field(default_factory=list)
    exclude_episode_ids: list[int] = field(default_factory=list)
    usage: str | None = None
    theme: str | None = None  # the theme's ID, kept so cursors are bound to it
    theme_clip_ids: list[int] | None = None  # its members, resolved by the caller
    ranking: vectors.Ranking | None = None


def matched_sql(f: ClipFilters, key_sql: str = "c.id", *, joins: str = "",
                columns: str = "") -> tuple[str, dict[str, Any]]:
    """SELECT of the matching clips as `_id`, with `_k` = key_sql, the match columns and `columns`, plus the
    filter params (query_params adds the values that aren't filters)."""
    ranking = f.ranking
    params: dict[str, Any] = {"ids": f.ids, "q": f.q, "episodes": f.episode_ids, "transcripts": f.transcript_ids,
                              "runs": f.run_ids, "speakers": f.speaker_ids, "min_score": f.min_score,
                              "min_d": f.min_duration_ms,
                              "max_d": f.max_duration_ms, "not_ids": f.exclude_ids,
                              "not_episodes": f.exclude_episode_ids, "theme": f.theme, "usage": f.usage}
    where = ["r.status = 'ready'"]
    if ranking:
        params.update(mode=ranking.mode, like=ranking.like, min_sim=ranking.min_similarity, model=ranking.model)
        if ranking.like:
            where.append("c.id <> ALL(%(like)s)")
        if ranking.min_similarity is not None:
            where.append("m.similarity >= %(min_sim)s")
    if f.ids:
        where.append("c.id = ANY(%(ids)s)")
    if f.episode_ids:
        where.append("e.id = ANY(%(episodes)s)")
    if f.transcript_ids:
        where.append("r.transcript_version_id = ANY(%(transcripts)s)")
    if f.run_ids:
        where.append("r.id = ANY(%(runs)s)")
    if f.speaker_ids:
        where.append("EXISTS (SELECT 1 FROM episode_speakers es WHERE es.episode_id = e.id "
                     "AND es.speaker_id = ANY(%(speakers)s))")
    if f.min_score is not None:
        where.append("c.overall_score >= %(min_score)s")
    if f.min_duration_ms is not None:
        where.append("c.end_ms - c.start_ms >= %(min_d)s")
    if f.max_duration_ms is not None:
        where.append("c.end_ms - c.start_ms <= %(max_d)s")
    if f.usage == "never":
        where.append(f"NOT {USED}")
    elif f.usage == "picked":
        where.append(USED)
    elif f.usage == "rendered":
        where.append(RENDERED)
    if f.theme_clip_ids is not None:
        where.append("c.id = ANY(%(theme_ids)s)")
    if f.exclude_ids:
        where.append("c.id <> ALL(%(not_ids)s)")
    if f.exclude_episode_ids:
        where.append("e.id <> ALL(%(not_episodes)s)")
    if ranking:
        match_join = RANKED_JOINS[ranking.mode]
        match_cols = ", m.similarity" + (", m.relevance, m.fused" if ranking.mode == "hybrid" else "")
    else:
        match_join, match_cols = (MATCH_JOIN, ", m.relevance") if f.q else ("", "")
    sql = f"""SELECT c.id AS _id, ({key_sql})::double precision AS _k{match_cols}{columns}
              {CLIP_FROM} {match_join} {joins} WHERE {' AND '.join(where)}"""
    return sql, params


def query_params(f: ClipFilters, params: dict[str, Any]) -> dict[str, Any]:
    """The values matched_sql's SQL needs beyond its filters: the query vector and the theme's members."""
    out = dict(params)
    if f.ranking:
        out["qv"] = f.ranking.vector
    if f.theme_clip_ids is not None:
        out["theme_ids"] = f.theme_clip_ids
    return out


def list_clips(conn: psycopg.Connection, f: ClipFilters, *, base: str, sort: str | None, include: set[str],
               limit: int, cursor: str | None, max_per_episode: int | None = None, diversify: bool = False,
               view: str = "full") -> tuple[list[dict], str | None]:
    ranking = f.ranking
    sort = sort or ("-relevance" if f.q or ranking else "-score")
    if sort == "-relevance" and not (f.q or ranking):
        raise bad_request([FieldError("invalid_value", "`-relevance` needs `q` or `like`.", parameter="sort")])
    key_sql, descending = SORTS[sort]
    if ranking:
        key_sql = RANKED_KEYS[ranking.mode]
    joins = USAGE_JOIN if "rendered" in sort else ""
    if max_per_episode:  # rank within each episode over the whole filtered set, so every page agrees
        order = "DESC" if descending else "ASC"
        rank = f", row_number() OVER (PARTITION BY e.id ORDER BY ({key_sql}) {order}, c.id {order}) AS _n"
        inner, params = matched_sql(f, key_sql, joins=joins, columns=rank)
        sql = f"SELECT * FROM ({inner}) per_episode WHERE _n <= %(per_episode)s"
    else:
        sql, params = matched_sql(f, key_sql, joins=joins)
    params.update(per_episode=max_per_episode, diversify=diversify)
    filters = {**params, "sort": sort, "view": view}  # the query vector isn't a filter: q or like pins it
    params = query_params(f, params)
    if f.ids:  # batch fetch: requested order, no pagination
        rows = conn.execute(sql, params).fetchall()
        by_id = {r["_id"]: r for r in rows}
        ordered = [by_id[i] for i in dict.fromkeys(f.ids) if i in by_id]
        next_cursor = None
    elif diversify:  # one re-ranked page from the best candidates; MMR can't resume from a cursor
        pool = conn.execute(f"SELECT * FROM ({sql}) q ORDER BY q._k DESC, q._id DESC LIMIT {vectors.DIVERSIFY_POOL}",
                            params).fetchall()
        similarity = vectors.pairwise(conn, [r["_id"] for r in pool], ranking.model)
        by_id = {r["_id"]: r for r in pool}
        relevance = cosine_scale([_number(r["similarity"]) for r in pool])
        ordered = [by_id[i] for i in mmr([(r["_id"], rel) for r, rel in zip(pool, relevance, strict=True)],
                                         similarity, limit)]
        next_cursor = None
    else:
        page = keyset_page(conn, sql, params, descending=descending, limit=limit, cursor=cursor, filters=filters)
        ordered, next_cursor = page.rows, page.next_cursor
    ids = [r["_id"] for r in ordered]
    compact = view == "compact"
    clips = load_compact(conn, ids, base) if compact else load_clips(conn, ids, include, base)
    if ranking:
        passages = {} if compact else vectors.passages_for(conn, ranking, ids, f.q)
        for clip, r in zip(clips, ordered, strict=True):
            match: dict[str, Any] = {"similarity": _number(r["similarity"])}
            if ranking.mode == "hybrid":
                match.update(relevance=_number(r["relevance"]), fused=float(r["fused"]))
            clip["match"] = match if compact else {**match, "passages": passages.get(clip["id"], [])}
    elif f.q:
        passages = {} if compact else matched_passages(conn, f.q, ids)
        for clip, r in zip(clips, ordered, strict=True):
            match = {"relevance": float(r["relevance"])}
            clip["match"] = match if compact else {**match, "passages": passages.get(clip["id"], [])}
    return clips, next_cursor


def _number(value) -> float | None:
    return None if value is None else float(value)


# group_by -> (key, label, join) over matched_sql's joins (c, r, e).
USAGE_GROUP = f"CASE WHEN {RENDERED} THEN 'rendered' WHEN {USED} THEN 'picked_unrendered' ELSE 'never' END"
FACET_KEYS = {
    "episode": ("e.id::text", "e.title", ""),
    # A clip counts once for each speaker of its episode; the LEFT join keeps clips with none in the total.
    "speaker": ("s.id::text", "s.name",
                "LEFT JOIN episode_speakers es ON es.episode_id = e.id LEFT JOIN speakers s ON s.id = es.speaker_id"),
    # Disjoint, unlike the usage filter, where `picked` includes rendered clips.
    "usage": (USAGE_GROUP, USAGE_GROUP, ""),
}
FACET_SORTS = {"-clips": "clip_count DESC", "-unused": "unused_count DESC, clip_count DESC"}


def facets(conn: psycopg.Connection, f: ClipFilters, *, group_by: str, sort: str, limit: int) -> dict:
    """Counts of the clips GET /clips would return with the same filters, grouped."""
    key, label, join = FACET_KEYS[group_by]
    inner, params = matched_sql(f, joins=join, columns=f", {key} AS _key, {label} AS _label, NOT {USED} AS _unused")
    params = query_params(f, params) | {"_limit": limit}
    rows = conn.execute(
        f"""WITH matched AS ({inner}),
                 counted AS (SELECT _key AS key, min(_label) AS label, count(DISTINCT _id) AS clip_count,
                                    count(DISTINCT _id) FILTER (WHERE _unused) AS unused_count
                             FROM matched WHERE _key IS NOT NULL GROUP BY _key)
            SELECT (SELECT count(DISTINCT _id) FROM matched) AS total, (SELECT count(*) FROM counted) AS group_count,
                   k.key, k.label, k.clip_count, k.unused_count
            FROM (SELECT 1) one LEFT JOIN LATERAL (
                SELECT * FROM counted ORDER BY {FACET_SORTS[sort]}, label, key LIMIT %(_limit)s) k ON TRUE""",
        params).fetchall()
    groups = [{"key": r["key"] if group_by == "usage" else int(r["key"]), "label": r["label"],
               "clip_count": r["clip_count"], "unused_count": r["unused_count"]} for r in rows if r["key"] is not None]
    return {"group_by": group_by, "clip_count": rows[0]["total"], "group_count": rows[0]["group_count"],
            "groups": groups}


CLIP_COLUMNS = """c.id, c.slug, c.run_id, c.title, c.summary, c.rank, c.overall_score, c.start_ms, c.end_ms,
    c.first_word_seq, c.last_word_seq, c.has_thumb, r.transcript_version_id AS transcript_id,
    e.id AS episode_id, e.title AS episode_title, e.published_at, e.platform, e.video_id, e.source_url,
    p.name AS podcast"""


def _summary(r: dict, base: str, usage: dict) -> dict:
    return {
        "id": r["id"], "run_id": r["run_id"], "title": r["title"], "summary": r["summary"], "rank": r["rank"],
        "score": r["overall_score"], "episode": episode_ref(r),
        "source": {"platform": r["platform"], "video_id": r["video_id"], "url": r["source_url"]},
        "transcript_id": r["transcript_id"],
        "words": {"first": r["first_word_seq"], "last": r["last_word_seq"]},
        "time": {"start_ms": r["start_ms"], "end_ms": r["end_ms"]}, "duration_ms": r["end_ms"] - r["start_ms"],
        "usage": usage, **clip_links(base, r),
    }


def load_compact(conn: psycopg.Connection, ids: list[int], base: str) -> list[dict]:
    """ClipSummary objects (no text or includes), in the order of `ids`. Missing ids are skipped."""
    if not ids:
        return []
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT {CLIP_COLUMNS} {CLIP_FROM} LEFT JOIN podcasts p ON p.id = e.podcast_id WHERE c.id = ANY(%s)",
        (ids,))}
    usage = usage_for(conn, list(rows))
    return [_summary(rows[i], base, usage[i]) for i in ids if i in rows]


def usage_for(conn: psycopg.Connection, clip_ids: list[int]) -> dict[int, dict]:
    out = {i: {"compilations": 0, "revisions": 0, "rendered": 0, "last_rendered_at": None} for i in clip_ids}
    if not clip_ids:
        return out
    for r in conn.execute(f"SELECT * FROM ({CLIP_USAGE_SQL}) u WHERE u.clip_id = ANY(%s)", (clip_ids,)):
        out[r["clip_id"]] = {"compilations": r["compilations"], "revisions": r["revisions"],
                             "rendered": r["rendered"], "last_rendered_at": iso(r["last_rendered_at"])}
    return out


def load_clips(conn: psycopg.Connection, ids: list[int], include: set[str], base: str) -> list[dict]:
    """Full Clip objects, in the order of `ids`: the compact form plus text and includes."""
    clips = load_compact(conn, ids, base)
    if not clips:
        return clips
    found = [c["id"] for c in clips]
    extra = {r["id"]: r for r in conn.execute(
        """SELECT c.id, c.details,
                  (SELECT string_agg(w.word, ' ' ORDER BY w.seq) FROM transcript_words w
                    WHERE w.transcript_version_id = r.transcript_version_id
                      AND w.seq BETWEEN c.first_word_seq AND c.last_word_seq) AS text
           FROM clips c JOIN runs r ON r.id = c.run_id WHERE c.id = ANY(%s)""", (found,))}
    quotes = quotes_for(conn, found) if "quotes" in include else {}
    speakers = speakers_by_episode(conn, list({c["episode"]["id"] for c in clips})) if "speakers" in include else {}
    out = []
    for clip in clips:
        row = extra[clip["id"]]
        full = {**{k: v for k, v in clip.items() if k not in ("usage", "share_url", "thumbnail_url", "links")},
                "text": row["text"] or ""}
        if "quotes" in include:
            full["quotes"] = quotes.get(clip["id"], [])
        if "speakers" in include:
            full["speakers"] = speakers.get(clip["episode"]["id"], [])
        if "details" in include:
            full["details"] = row["details"]
        out.append({**full, **{k: clip[k] for k in ("usage", "share_url", "thumbnail_url", "links")}})
    return out


def run_clip_ids(conn: psycopg.Connection, run_id: int) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM clips WHERE run_id = %s ORDER BY rank, id", (run_id,))]


def get_clip(conn: psycopg.Connection, clip_id: int, include: set[str], base: str) -> dict:
    found = load_clips(conn, [clip_id], include, base)
    if not found:
        raise not_found("Clip")
    return found[0]


def download_source(conn: psycopg.Connection, clip_id: int) -> dict:
    row = conn.execute(f"SELECT e.video_id, c.start_ms, c.end_ms {CLIP_FROM} WHERE c.id = %s", (clip_id,)).fetchone()
    if row is None:
        raise not_found("Clip")
    return row


def get_shared(conn: psycopg.Connection, slug: str, include: set[str], base: str) -> dict:
    row = conn.execute("SELECT id FROM clips WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        raise not_found("Clip")
    return get_clip(conn, row["id"], include, base)


def quotes_for(conn: psycopg.Connection, clip_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in conn.execute(
        """SELECT clip_id, ordinal, quote_text, first_word_seq, last_word_seq FROM clip_quotes
           WHERE clip_id = ANY(%s) ORDER BY clip_id, ordinal""", (clip_ids,)):
        words = ({"first": r["first_word_seq"], "last": r["last_word_seq"]}
                 if r["first_word_seq"] is not None and r["last_word_seq"] is not None else None)
        out.setdefault(r["clip_id"], []).append({"ordinal": r["ordinal"], "text": r["quote_text"], "words": words})
    return out


def matched_passages(conn: psycopg.Connection, q: str, clip_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if not clip_ids:
        return out
    for r in conn.execute(
        """SELECT p.clip_id, p.first_word_seq, p.last_word_seq, p.text
           FROM passages p, websearch_to_tsquery('english', %s) query
           WHERE p.clip_id = ANY(%s) AND to_tsvector('english', p.text) @@ query
           ORDER BY p.clip_id, p.first_word_seq""", (q, clip_ids)):
        out.setdefault(r["clip_id"], []).append(
            {"words": {"first": r["first_word_seq"], "last": r["last_word_seq"]}, "text": r["text"]})
    return out


def clip_context(conn: psycopg.Connection, clip_id: int, before_words: int, after_words: int) -> dict:
    """The words just before and after a clip, read from the transcript."""
    row = conn.execute(
        """SELECT c.id, c.first_word_seq, c.last_word_seq, r.transcript_version_id AS transcript_id, tv.punctuated
           FROM clips c JOIN runs r ON r.id = c.run_id JOIN transcript_versions tv ON tv.id = r.transcript_version_id
           WHERE c.id = %s""", (clip_id,)).fetchone()
    if row is None:
        raise not_found("Clip")
    first, last = row["first_word_seq"], row["last_word_seq"]
    parts = {"before": [], "clip": [], "after": []}
    for w in conn.execute(
        """SELECT seq, word FROM transcript_words WHERE transcript_version_id = %s AND seq BETWEEN %s AND %s
           ORDER BY seq""", (row["transcript_id"], max(1, first - before_words), last + after_words)):
        parts["before" if w["seq"] < first else "after" if w["seq"] > last else "clip"].append(w["word"])
    before, clip, after = (" ".join(parts[k]) for k in ("before", "clip", "after"))
    return {
        "clip_id": clip_id, "transcript_id": row["transcript_id"], "before": before, "clip": clip, "after": after,
        # Without punctuation (YouTube auto-captions) these can't be told, so they're null, not a guess.
        "alignment": "exact",
        "starts_mid_sentence": ctx.starts_mid_sentence(before) if row["punctuated"] else None,
        "follows_question": ctx.follows_question(before) if row["punctuated"] else None,
    }


def clip_words(conn: psycopg.Connection, clip_ids: list[int]) -> dict[int, ClipWords]:
    """Bounds and timed words for each clip (the input to segment resolution)."""
    out: dict[int, ClipWords] = {}
    if not clip_ids:
        return out
    for r in conn.execute(
        """SELECT c.id, r.transcript_version_id, c.first_word_seq, c.last_word_seq
           FROM clips c JOIN runs r ON r.id = c.run_id WHERE c.id = ANY(%s) AND r.status = 'ready'""",
            (clip_ids,)):
        out[r["id"]] = ClipWords(r["id"], r["transcript_version_id"], r["first_word_seq"], r["last_word_seq"])
    for r in conn.execute(
        """SELECT c.id AS clip_id, w.seq, w.word, w.start_ms, w.end_ms
           FROM clips c JOIN runs r ON r.id = c.run_id
           JOIN transcript_words w ON w.transcript_version_id = r.transcript_version_id
            AND w.seq BETWEEN c.first_word_seq AND c.last_word_seq
           WHERE c.id = ANY(%s)""", (list(out),)):
        out[r["clip_id"]].words[r["seq"]] = Word(r["seq"], r["word"], r["start_ms"], r["end_ms"])
    return out


def appearances(conn: psycopg.Connection, clip_id: int, limit: int,
                cursor: str | None) -> tuple[list[dict], str | None]:
    """Every revision item that uses the clip, newest first."""
    if conn.execute("SELECT 1 FROM clips WHERE id = %s", (clip_id,)).fetchone() is None:
        raise not_found("Clip")
    sql = f"""
        SELECT co.id::double precision * 100000 + ci."position" AS _k,
               co.id * 100000 + ci."position" AS _id, co.id AS revision_id,
               (SELECT min(x.id) FROM compilations x WHERE x.title = co.title) AS compilation_id,
               co.revision AS number, co.title, ci."position", ci.first_word_seq, ci.last_word_seq,
               co.id IN (SELECT id FROM ({LATEST_SQL}) l) AS is_latest,
               COALESCE((SELECT array_agg(rn.id ORDER BY rn.id) FROM compilation_renders rn
                          JOIN compilation_render_positions rp ON rp.render_id = rn.id AND rp."position" = ci."position"
                         WHERE rn.revision_id = co.id), '{{}}') AS render_ids
        FROM compilation_items ci JOIN compilations co ON co.id = ci.compilation_id
        WHERE ci.clip_id = %(clip)s"""
    page = keyset_page(conn, sql, {"clip": clip_id}, descending=True, limit=limit, cursor=cursor,
                       filters={"clip": clip_id})
    return [{
        "revision_id": r["revision_id"], "compilation_id": r["compilation_id"], "number": r["number"],
        "title": r["title"], "position": r["position"],
        "words": {"first": r["first_word_seq"], "last": r["last_word_seq"]},
        "is_latest": r["is_latest"], "render_ids": list(r["render_ids"]),
    } for r in page.rows], page.next_cursor
