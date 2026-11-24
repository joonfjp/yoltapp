"""Themes over the clip vectors: clustered on demand, cached until the vectors change.

Ported from clipit_api.

A clip is clustered by what is said in it: the mean of its passages' vectors. Clip vectors embed
titles and summaries, whose punchy house style outweighed the subject when clustered (funny
anecdotes grouped by tone); a clip vector is used only for a clip with no embedded passage.
Clustering takes a fraction of a second, so it's done once per (model, k, vectors) and kept in
memory; vectors travel in pgvector's binary form. Usage and coverage are read on every request.
A theme's ID is `k{k}-{medoid clip}-{vectors}`: the k, its most central clip, and four hex digits of
the vectors' fingerprint. It says which clustering it belongs to, so `/clips?theme=` needs no other
parameter, and it stops resolving the moment the vectors change.

Looking a theme up clusters again (once per key, as GET /themes does) only when its fingerprint digits
match the current vectors: an ID listed before a restart or an eviction still resolves, and a made-up
one usually costs a fingerprint query, not a clustering. Zero vectors aren't clustered.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import psycopg

from ..domain import themes as clustering
from ..embeddings import EmbeddingService
from ..problems import FieldError, bad_request, invalid
from . import vectors
from .common import LATEST_SQL

DEFAULT_K = 30
MIN_K = 2
MAX_K = 100
# The k's the UI asks for by default (app.js HOME_THEMES_K, loadThemes): the home page's 12 and Search's 20, or
# every clip when there are fewer. The worker clusters these ahead (warm) once new vectors are in.
UI_KS = (12, 20)
TOP_COMPILATIONS = 3
COVERED_BY = 2  # a compilation covers a theme when at least this many of its items come from it
THEME_ID = re.compile(r"^k([1-9]\d*)-([1-9]\d*)-([0-9a-f]{4})$")
SORTS = {"-unused": lambda t: (-t["unused_count"], -t["clip_count"]),
         "-clips": lambda t: (-t["clip_count"],),
         "-cohesion": lambda t: (-t["cohesion"],),
         # least covered first, then the most unused material
         "coverage": lambda t: (t["coverage"]["compilation_count"], -t["unused_count"])}

# Each clustered clip's vector: its nonzero passages' mean, else its own. A zero vector has no direction
# (cosine with it is NaN), so it would only make a junk theme of its own. Sent as pgvector binary (vector_send).
# Worked out clip by clip (an index lookup or two each), so no step ever sorts or hashes rows carrying 4 KB
# vectors: grouped all at once they spilled to disk, ~2x slower on 24,000 clips. A clip's own vector is only
# looked up when it has no passage vectors, and OFFSET 0 keeps the subquery from being merged into the outer
# one, which would work each clip's vector out twice (once more for the WHERE).
VECTORS_SQL = """
SELECT clip_id, title, summary, episode_id, vector_send(v) AS v FROM (
    SELECT c.id AS clip_id, c.title, c.summary, r.episode_id, COALESCE(
        (SELECT avg(pv.embedding) FROM passages p
           JOIN passage_vectors pv ON pv.passage_id = p.id AND pv.model_name = %(m)s
          WHERE p.clip_id = c.id AND vector_norm(pv.embedding) > 0),
        (SELECT cv.embedding FROM clip_vectors cv
          WHERE cv.clip_id = c.id AND cv.model_name = %(m)s AND vector_norm(cv.embedding) > 0)) AS v
    FROM clips c JOIN runs r ON r.id = c.run_id
    OFFSET 0
) x
WHERE vector_norm(v) > 0
ORDER BY clip_id"""

# Changes whenever a vector the clustering reads does, without reading (or hashing) the vectors: rows
# are only upserted, which stamps embedded_at = now(), or deleted, so per table the row count, the sum
# of keys and the latest embedded_at tell. Clips and passages never change once stored. `n`, the clips
# with a vector, counts zero vectors too: it bounds k, and a clustering checks the rest.
FINGERPRINT_SQL = """SELECT
    (SELECT count(*) FROM clips c WHERE EXISTS (SELECT 1 FROM clip_vectors v WHERE v.clip_id = c.id AND v.model_name = %(m)s)
        OR EXISTS (SELECT 1 FROM passages p JOIN passage_vectors pv ON pv.passage_id = p.id AND pv.model_name = %(m)s
                   WHERE p.clip_id = c.id)) AS n,
    md5(concat_ws('|',
        (SELECT concat_ws(':', count(*), sum(clip_id), extract(epoch FROM max(embedded_at)))
         FROM clip_vectors WHERE model_name = %(m)s),
        (SELECT concat_ws(':', count(*), sum(passage_id), extract(epoch FROM max(embedded_at)))
         FROM passage_vectors WHERE model_name = %(m)s))) AS digest
"""

@dataclass(frozen=True)
class Theme:
    id: str
    clip_ids: list[int]  # nearest the centroid first
    keywords: list[str]
    representatives: list[dict]
    episode_count: int
    cohesion: float


class TooFewClips(Exception):
    """Fewer clips can be clustered than k (zero vectors aren't)."""

    def __init__(self, count: int):
        super().__init__(count)
        self.count = count


_cache: OrderedDict[tuple, list[Theme]] = OrderedDict()
_builds: dict[tuple, threading.Lock] = {}  # one build per key at a time; later requests wait for it
_lock = threading.Lock()
CACHE_SIZE = 8  # every k the UI offers (5, 10, 20, 30, 50), and a few more


@dataclass(frozen=True)
class Fingerprint:
    model: str
    count: int
    digest: str

    def key(self, k: int) -> tuple:
        return (self.model, k, self.count, self.digest)


def fingerprint(conn: psycopg.Connection, service: EmbeddingService) -> Fingerprint:
    model = service.model_name
    vectors.require_vectors(conn, model, "Themes")
    fp = conn.execute(FINGERPRINT_SQL, {"m": model}).fetchone()
    return Fingerprint(model, fp["n"], fp["digest"])


def _build(conn: psycopg.Connection, fp: Fingerprint, k: int, rows: list[dict] | None = None) -> list[Theme]:
    """Cluster into k themes. `rows` are VECTORS_SQL's, if the caller has read them already."""
    if rows is None:
        rows = conn.execute(VECTORS_SQL, {"m": fp.model}).fetchall()
    if len(rows) < k:
        raise TooFewClips(len(rows))
    result = clustering.cluster(np.stack([from_binary(r["v"]) for r in rows]), k)
    groups: list[list[int]] = [[] for _ in range(k)]
    for row, label in enumerate(result.labels):
        groups[label].append(row)
    groups = [sorted(g, key=lambda i: (-result.similarity[i], rows[i]["clip_id"])) for g in groups]
    words = clustering.keywords([[f"{rows[i]['title']} {rows[i]['summary'] or ''}" for i in g] for g in groups])
    themes = []
    for j, g in enumerate(groups):
        if not g:
            continue
        themes.append(Theme(
            id=f"k{k}-{rows[g[0]]['clip_id']}-{fp.digest[:4]}", clip_ids=[rows[i]["clip_id"] for i in g],
            keywords=words[j],
            representatives=[{"id": rows[i]["clip_id"], "title": rows[i]["title"], "summary": rows[i]["summary"]}
                             for i in g[:clustering.REPRESENTATIVES]],
            episode_count=len({rows[i]["episode_id"] for i in g}),
            cohesion=float(np.mean(result.similarity[g]))))
    return themes


def themes_for(conn: psycopg.Connection, service: EmbeddingService, k: int, fp: Fingerprint | None = None) -> list[Theme]:
    fp = fp or fingerprint(conn, service)
    try:
        if k > fp.count:
            raise TooFewClips(fp.count)
        return _clustered(conn, fp, k)
    except TooFewClips as few:
        raise bad_request([FieldError("invalid_value", f"`k` can be at most the {few.count} embedded clips.",
                                      parameter="k")]) from None


def _clustered(conn: psycopg.Connection, fp: Fingerprint, k: int, rows: list[dict] | None = None) -> list[Theme]:
    """The clustering for this key, from the cache or built now (from `rows`, if given). Raises TooFewClips."""
    key = fp.key(k)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        build = _builds.setdefault(key, threading.Lock())
    with build:  # concurrent cold requests for one key share a single clustering
        with _lock:
            if key in _cache:
                return _cache[key]
        try:
            built = _build(conn, fp, k, rows)
        except BaseException:
            with _lock:
                _builds.pop(key, None)
            raise
        with _lock:
            _cache[key] = built
            _builds.pop(key, None)
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    return built


def warm(conn: psycopg.Connection, service: EmbeddingService) -> list[int]:
    """Cluster the themes the UI shows by default (UI_KS) for the current vectors, unless they're cached: so the
    first look after new vectors (or a restart) needn't wait for it, a second or more in a large catalogue. The
    vectors are read once for all of them. Returns the k's clustered now; none when nothing is embedded."""
    if not vectors.has_vectors(conn, service.model_name):
        return []
    fp = fingerprint(conn, service)
    if fp.count < MIN_K:
        return []
    with _lock:
        due = sorted(k for k in {min(k, fp.count) for k in UI_KS} if fp.key(k) not in _cache)
    rows = conn.execute(VECTORS_SQL, {"m": fp.model}).fetchall() if due else []
    built = []
    for k in due:
        try:
            _clustered(conn, fp, k, rows)
        except TooFewClips:  # some clips with vectors have only zero ones: fewer to cluster than counted
            continue
        built.append(k)
    return built


def members(conn: psycopg.Connection, service: EmbeddingService, theme_id: str) -> list[int]:
    """The clip IDs of a theme, for /clips?theme=. 400 if it isn't a theme ID, 422 if the current clustering
    doesn't have it: the vectors changed. An ID of the current vectors is clustered again if need be."""
    m = THEME_ID.match(theme_id)
    if m is None or int(m.group(1)) > MAX_K:
        raise bad_request([FieldError("invalid_value", "A theme ID looks like `k30-1911-3fa2`; get one from GET /themes.",
                                      parameter="theme")])
    k = int(m.group(1))
    fp = fingerprint(conn, service)
    theme = None
    if fp.digest.startswith(m.group(3)) and MIN_K <= k <= fp.count:
        try:
            theme = next((t for t in _clustered(conn, fp, k) if t.id == theme_id), None)
        except TooFewClips:
            pass
    if theme is None:
        raise invalid([FieldError("theme_not_found", f"No theme {theme_id} in the current clustering; the vectors may "
                                  "have changed. List GET /themes again.", parameter="theme")])
    return theme.clip_ids


def from_binary(data: bytes) -> np.ndarray:
    """pgvector's binary form: dimension and a reserved int16, then big-endian float32s."""
    dim = int.from_bytes(data[:2], "big")
    return np.frombuffer(data, dtype=">f4", count=dim, offset=4).astype(np.float64)


# Every compilation's latest revision, with its compilation ID, title and the clips of its items.
COVERAGE_SQL = f"""
SELECT f.compilation_id, l.title, ci.clip_id
FROM ({LATEST_SQL}) l
JOIN (SELECT title, min(id) AS compilation_id FROM compilations GROUP BY title) f ON f.title = l.title
JOIN compilation_items ci ON ci.compilation_id = l.id"""


def used_clip_ids(conn: psycopg.Connection) -> set[int]:
    """Clips some revision has picked."""
    return {r["clip_id"] for r in conn.execute("SELECT DISTINCT clip_id FROM compilation_items")}


def covering(conn: psycopg.Connection, themes: list[Theme]) -> dict[int, list[tuple[int, int, str]]]:
    """Per theme index, the compilations covering it as (item_count, id, title), most items first. A
    compilation covers a theme when at least COVERED_BY items of its latest revision come from the theme."""
    home = {clip: n for n, t in enumerate(themes) for clip in t.clip_ids}
    counts: dict[tuple[int, int], int] = {}
    titles: dict[int, str] = {}
    for r in conn.execute(COVERAGE_SQL):
        titles[r["compilation_id"]] = r["title"]
        if r["clip_id"] in home:
            key = (home[r["clip_id"]], r["compilation_id"])
            counts[key] = counts.get(key, 0) + 1
    out: dict[int, list[tuple[int, int, str]]] = {}
    for (theme, cid), count in counts.items():
        if count >= COVERED_BY:
            out.setdefault(theme, []).append((count, cid, titles[cid]))
    return {theme: sorted(found, key=lambda c: (-c[0], c[1])) for theme, found in out.items()}


def describe(conn: psycopg.Connection, themes: list[Theme]) -> list[dict]:
    """Themes as JSON, with usage and coverage read now."""
    used = used_clip_ids(conn)
    cover = covering(conn, themes)
    out = []
    for n, t in enumerate(themes):
        comps = cover.get(n, [])
        out.append({
            "id": t.id, "keywords": t.keywords, "representatives": t.representatives,
            "clip_count": len(t.clip_ids), "unused_count": sum(i not in used for i in t.clip_ids),
            "episode_count": t.episode_count, "cohesion": t.cohesion,
            "coverage": {"compilation_count": len(comps),
                         "compilations": [{"id": cid, "title": title, "item_count": count}
                                          for count, cid, title in comps[:TOP_COMPILATIONS]]}})
    return out


def list_themes(conn: psycopg.Connection, service: EmbeddingService, k: int, sort: str) -> dict:
    themes = themes_for(conn, service, k)
    data = sorted(describe(conn, themes), key=lambda t: (*SORTS[sort](t), t["id"]))
    return {"k": k, "clip_count": sum(len(t.clip_ids) for t in themes), "data": data}
