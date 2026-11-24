"""Semantic ranking over clip_vectors and passage_vectors (written by yolt/embed.py).

A clip's similarity to a query is the best of its own vector and its passages' vectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from ..embed import EMBEDDED_CLIP
from ..embeddings import MAX_QUERY_CHARS, EmbeddingService, EmbeddingUnavailable, literal
from ..problems import ApiProblem, ErrorList, FieldError, invalid
from .common import iso

# passages.fts is the stored to_tsvector('english', text) (migration 0017), so ranking doesn't re-tokenize.
KEYWORD_SQL = """SELECT p.clip_id, max(ts_rank_cd(p.fts, query)) AS relevance
    FROM passages p, websearch_to_tsquery('english', %(q)s) query
    WHERE p.fts @@ query
    GROUP BY p.clip_id"""

# Cosine distance with an all-zero vector is NaN (which would sort first and can't be sent as JSON), so
# zero vectors, stored or queried, never match. pgvector's vector_norm is immutable: the query's is computed once.
NONZERO = "vector_norm({v}) > 0 AND vector_norm(%(qv)s::vector) > 0"
SIMILARITY_SQL = f"""SELECT clip_id, max(similarity) AS similarity FROM (
        SELECT clip_id, 1 - (embedding <=> %(qv)s::vector) AS similarity
        FROM clip_vectors WHERE model_name = %(model)s AND {NONZERO.format(v="embedding")}
        UNION ALL
        SELECT p.clip_id, 1 - (pv.embedding <=> %(qv)s::vector)
        FROM passage_vectors pv JOIN passages p ON p.id = pv.passage_id
        WHERE pv.model_name = %(model)s AND {NONZERO.format(v="pv.embedding")}
    ) s GROUP BY clip_id"""

# Reciprocal rank fusion: each list contributes 1 / (RRF_K + rank). Ranks are over the whole catalogue.
RRF_K = 60
HYBRID_SQL = f"""SELECT COALESCE(k.clip_id, s.clip_id) AS clip_id, k.relevance, s.similarity,
           (COALESCE(1.0 / ({RRF_K} + k.n), 0) + COALESCE(1.0 / ({RRF_K} + s.n), 0))::double precision AS fused
    FROM (SELECT clip_id, relevance, row_number() OVER (ORDER BY relevance DESC, clip_id) AS n
          FROM ({KEYWORD_SQL}) x) k
    FULL JOIN (SELECT clip_id, similarity, row_number() OVER (ORDER BY similarity DESC, clip_id) AS n
               FROM ({SIMILARITY_SQL}) y) s ON s.clip_id = k.clip_id"""

TOP_PASSAGES = 3
MAX_LIKE = 50
DIVERSIFY_POOL = 200  # candidates diversify=true re-ranks


@dataclass(frozen=True)
class Ranking:
    """How a semantic list is ordered: `mode` is semantic, hybrid or like."""
    mode: str
    vector: str
    model: str
    like: list[int] = field(default_factory=list)
    min_similarity: float | None = None


def _embed_query(service: EmbeddingService, q: str) -> str:
    try:
        return literal(service.query(q))
    except EmbeddingUnavailable as exc:
        raise ApiProblem(503, "embedding-unavailable", "Semantic search is unavailable", detail=str(exc)) from exc


def plan_clips(conn: psycopg.Connection, service: EmbeddingService, *, q: str | None, search: str | None,
               like: list[int], min_similarity: float | None, sort: str | None, diversify: bool = False,
               cursor: str | None = None, facets: bool = False) -> Ranking | None:
    """Validate the semantic parameters of GET /clips (or its facets); None means keyword search or browsing."""
    clip_query_errors(q=q, search=search, like=like, min_similarity=min_similarity, sort=sort, diversify=diversify,
                      cursor=cursor, facets=facets).raise_if_any(400)
    mode = _mode(search, like)
    if mode is None:
        return None
    model = service.model_name
    if mode != "like":  # `like` says which liked clip has no vector: a 422 not_embedded
        require_vectors(conn, model)
    vector = _centroid(conn, like, model) if mode == "like" else _embed_query(service, q)
    return Ranking(mode, vector, model, like, min_similarity)


def _mode(search: str | None, like: list[int]) -> str | None:
    return "like" if like else (search if search in ("semantic", "hybrid") else None)


def clip_query_errors(*, q: str | None, search: str | None, like: list[int], min_similarity: float | None,
                      sort: str | None, diversify: bool, cursor: str | None, facets: bool) -> ErrorList:
    """What plan_clips refuses with a 400, without a connection: GET /clips checks it before embedding a query."""
    mode = _mode(search, like)
    errors = ErrorList()
    if like and q:
        errors.add("invalid_value", "`like` ranks by similarity to clips; it can't be combined with `q`.",
                   parameter="like")
    if search not in (None, "keyword") and not q:
        errors.add("invalid_value", f"`search={search}` needs `q`.", parameter="search")
    if mode and q and len(q) > MAX_QUERY_CHARS:
        errors.add("invalid_value", f"A semantic query can be at most {MAX_QUERY_CHARS} characters.", parameter="q")
    if min_similarity is not None and mode is None:
        errors.add("invalid_value", "`min_similarity` needs `search=semantic`, `search=hybrid` or `like`.",
                   parameter="min_similarity")
    if mode and sort not in (None, "-relevance"):
        errors.add("invalid_value", f"Semantic results are ordered by `-relevance`; `{sort}` isn't available.",
                   parameter="sort")
    if facets and mode and min_similarity is None:  # semantic ranking keeps every clip; counts need a cut-off
        errors.add("invalid_value", "Semantic, hybrid and `like` facets count every embedded clip; set "
                   "`min_similarity`.", parameter="min_similarity")
    if diversify and mode is None:
        errors.add("invalid_value", "`diversify` needs `search=semantic`, `search=hybrid` or `like`.",
                   parameter="diversify")
    elif diversify and cursor:
        errors.add("invalid_value", "Diversified results are a single page; there is no next page.",
                   parameter="cursor")
    return errors


def plan_compilations(conn: psycopg.Connection, service: EmbeddingService, *, q: str | None, search: str | None,
                      min_similarity: float | None) -> Ranking | None:
    """`search=semantic` on GET /compilations ranks them by their clips' mean vector; keyword matches titles."""
    errors = ErrorList()
    semantic = search == "semantic"
    if search not in (None, "keyword") and not q:
        errors.add("invalid_value", f"`search={search}` needs `q`.", parameter="search")
    if semantic and q and len(q) > MAX_QUERY_CHARS:
        errors.add("invalid_value", f"A semantic query can be at most {MAX_QUERY_CHARS} characters.", parameter="q")
    if min_similarity is not None and not semantic:
        errors.add("invalid_value", "`min_similarity` needs `search=semantic`.", parameter="min_similarity")
    errors.raise_if_any(400)
    if not semantic:
        return None
    require_vectors(conn, service.model_name)
    return Ranking("semantic", _embed_query(service, q), service.model_name, min_similarity=min_similarity)


def _centroid(conn: psycopg.Connection, like: list[int], model: str) -> str:
    """The mean of the liked clips' own vectors (cosine ignores its length)."""
    rows = conn.execute(
        """SELECT c.id, v.clip_id IS NOT NULL AS embedded FROM clips c
           LEFT JOIN clip_vectors v ON v.clip_id = c.id AND v.model_name = %s WHERE c.id = ANY(%s)""",
        (model, like)).fetchall()
    found = {r["id"]: r["embedded"] for r in rows}
    errors = [FieldError("clip_not_found", f"Clip {i} doesn't exist.", parameter="like") for i in like if i not in found]
    errors += [FieldError("not_embedded", f"Clip {i} has no vector yet.", parameter="like")
               for i in like if found.get(i) is False]
    if errors:
        raise invalid(errors)
    return conn.execute("SELECT avg(embedding)::text AS v FROM clip_vectors WHERE model_name = %s AND clip_id = ANY(%s)"
                        " AND vector_norm(embedding) > 0", (model, like)).fetchone()["v"] or literal([0.0] * 1024)


def pairwise(conn: psycopg.Connection, clip_ids: list[int], model: str) -> dict[tuple[int, int], float]:
    """Similarity of every pair of these clips' own vectors, keyed both ways."""
    out: dict[tuple[int, int], float] = {}
    for r in conn.execute(
        """SELECT a.clip_id AS a, b.clip_id AS b, 1 - (a.embedding <=> b.embedding) AS s
           FROM clip_vectors a JOIN clip_vectors b ON a.clip_id < b.clip_id
           WHERE a.model_name = %(m)s AND b.model_name = %(m)s
             AND a.clip_id = ANY(%(ids)s) AND b.clip_id = ANY(%(ids)s)
             AND vector_norm(a.embedding) > 0 AND vector_norm(b.embedding) > 0""", {"m": model, "ids": clip_ids}):
        out[(r["a"], r["b"])] = out[(r["b"], r["a"])] = float(r["s"])
    return out


def passages_for(conn: psycopg.Connection, ranking: Ranking, clip_ids: list[int],
                 q: str | None = None) -> dict[int, list[dict]]:
    """Each clip's nearest passages, with their similarity. Hybrid also keeps every keyword-matched passage."""
    if not clip_ids:
        return {}
    keyword = "p.fts @@ websearch_to_tsquery('english', %(q)s)" if ranking.mode == "hybrid" else "FALSE"
    rows = conn.execute(
        f"""SELECT p.clip_id, p.first_word_seq, p.last_word_seq, p.text, {keyword} AS keyword,
                   1 - (pv.embedding <=> %(qv)s::vector) AS similarity
            FROM passages p
            LEFT JOIN passage_vectors pv ON pv.passage_id = p.id AND pv.model_name = %(model)s
                 AND {NONZERO.format(v="pv.embedding")}
            WHERE p.clip_id = ANY(%(ids)s)
            ORDER BY p.clip_id, similarity DESC NULLS LAST, p.first_word_seq""",
        {"q": q, "qv": ranking.vector, "model": ranking.model, "ids": clip_ids}).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        kept = out.setdefault(r["clip_id"], [])
        nearest = r["similarity"] is not None and sum(p["similarity"] is not None for p in kept) < TOP_PASSAGES
        if nearest or r["keyword"]:
            kept.append({"words": {"first": r["first_word_seq"], "last": r["last_word_seq"]}, "text": r["text"],
                         "similarity": None if r["similarity"] is None else float(r["similarity"])})
    return out


def coverage(conn: psycopg.Connection, service: EmbeddingService) -> dict:
    """What semantic search can see right now, for GET /."""
    model = service.model_name
    # No clip vector is ever stale: clips never change once stored (so yolt/embed.py never re-embeds one).
    r = conn.execute(
        f"""SELECT (SELECT count(*) FROM clips) AS clips_total,
                   (SELECT count(*) FROM clip_vectors WHERE model_name = %(m)s) AS clips_embedded,
                   0 AS clips_stale,
                   (SELECT count(*) FROM passages) AS passages_total,
                   (SELECT count(*) FROM passage_vectors WHERE model_name = %(m)s) AS passages_embedded,
                   (SELECT count(*) FROM passage_vectors v JOIN passages p ON p.id = v.passage_id
                     WHERE v.model_name = %(m)s AND v.text_digest <> p.text_digest) AS passages_stale,
                   (SELECT max(t) FROM (SELECT max(embedded_at) AS t FROM clip_vectors WHERE model_name = %(m)s
                                        UNION ALL
                                        SELECT max(embedded_at) FROM passage_vectors WHERE model_name = %(m)s) x)
                     AS latest,
                   (SELECT count(*) FROM clips c WHERE NOT {EMBEDDED_CLIP}) AS pending_clips,
                   (SELECT last_error FROM embedding_status) AS last_error,
                   (SELECT last_error_at FROM embedding_status) AS last_error_at""", {"m": model}).fetchone()
    return {
        "model": model,
        "query_embedding": "configured" if service.configured else "not_configured",
        "pending_clips": r["pending_clips"],
        "last_error": {"message": r["last_error"], "at": iso(r["last_error_at"])} if r["last_error"] else None,
        "vectors": {kind: {"total": r[f"{kind}_total"], "embedded": r[f"{kind}_embedded"],
                           "stale": r[f"{kind}_stale"]} for kind in ("clips", "passages")}
        | {"latest_embedded_at": iso(r["latest"])},
    }


def has_vectors(conn: psycopg.Connection, model: str) -> bool:
    return conn.execute("SELECT 1 FROM clip_vectors WHERE model_name = %s LIMIT 1", (model,)).fetchone() is not None


def require_vectors(conn: psycopg.Connection, model: str, what: str = "Semantic search") -> None:
    if not has_vectors(conn, model):
        raise ApiProblem(503, "vectors-unavailable", f"{what} is unavailable",
                         detail=f"No clip has a {model} vector yet. They're made after each run; "
                                "`make embed` catches up.")
