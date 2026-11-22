"""Compilations and their immutable revisions (ported from clipit_api).

A revision is a row in `compilations`. A compilation is every row sharing a title, ordered by
`revision`; its ID is its first revision's ID, as in clipit.
"""
from __future__ import annotations


import psycopg
from psycopg.types.json import Jsonb

from ..domain import diff as diffs
from ..domain.segments import Resolved, check_overlaps, resolve
from ..problems import ApiProblem, ErrorList, FieldError, invalid, not_found, pointer
from .clips import clip_words
from .common import episode_ref, iso, keyset_page, speakers_by_episode

# Every revision with its compilation ID, latest number and predecessor, derived from titles.
REVISIONS = """
SELECT co.id, co.title, co.revision, co.created_at, co.metadata,
       min(co.id) OVER (PARTITION BY co.title) AS compilation_id,
       max(co.revision) OVER (PARTITION BY co.title) AS latest_number,
       first_value(co.id) OVER (PARTITION BY co.title ORDER BY co.revision DESC, co.id DESC) AS latest_id,
       count(*) OVER (PARTITION BY co.title) AS revision_count,
       lag(co.revision) OVER (PARTITION BY co.title ORDER BY co.revision, co.id) AS based_on
FROM compilations co"""

# The same for one compilation only: the revisions sharing revision %(of)s's title. Looking up one
# revision or compilation filters this, not REVISIONS, so the windows don't run over every revision.
LINEAGE = REVISIONS + " WHERE co.title = (SELECT x.title FROM compilations x WHERE x.id = %(of)s)"


def _summaries(revisions: str) -> str:
    return f"""
SELECT r.*, r.id = r.latest_id AS is_latest,
       (SELECT count(*) FROM compilation_items ci WHERE ci.compilation_id = r.id) AS item_count,
       (SELECT COALESCE(sum(ci.end_ms - ci.start_ms), 0) FROM compilation_items ci WHERE ci.compilation_id = r.id) AS duration_ms,
       (SELECT count(*) FROM compilation_renders rn WHERE rn.revision_id = r.id) AS render_count,
       (SELECT max(rn.rendered_at) FROM compilation_renders rn WHERE rn.revision_id = r.id) AS last_rendered_at
FROM ({revisions}) r"""


SUMMARY_SQL = _summaries(REVISIONS)
LINEAGE_SUMMARY_SQL = _summaries(LINEAGE)


def summary_json(r: dict) -> dict:
    return {
        "id": r["id"], "compilation_id": r["compilation_id"], "number": r["revision"], "title": r["title"],
        "based_on": r["based_on"], "created_at": iso(r["created_at"]), "metadata": r["metadata"],
        "item_count": r["item_count"], "duration_ms": int(r["duration_ms"]),
        "stats": {"render_count": r["render_count"], "last_rendered_at": iso(r["last_rendered_at"])},
    }


def revision_row(conn: psycopg.Connection, revision_id: int) -> dict:
    row = conn.execute(LINEAGE_SUMMARY_SQL + " WHERE r.id = %(of)s", {"of": revision_id}).fetchone()
    if row is None:
        raise not_found("Revision")
    return row


def _compilation(conn: psycopg.Connection, compilation_id: int) -> dict:
    """The compilation's latest revision row, or 404. Only a first-revision ID names a compilation."""
    row = conn.execute(f"SELECT * FROM ({LINEAGE}) r WHERE r.compilation_id = %(of)s ORDER BY r.revision DESC, r.id DESC LIMIT 1",
                       {"of": compilation_id}).fetchone()
    if row is None:
        raise not_found("Compilation")
    return row


def revision_id_for(conn: psycopg.Connection, compilation_id: int, number: int | str) -> int:
    if number == "latest":
        return _compilation(conn, compilation_id)["id"]
    row = conn.execute(f"SELECT id FROM ({LINEAGE}) r WHERE r.compilation_id = %(of)s AND r.revision = %(n)s ORDER BY id LIMIT 1",
                       {"of": compilation_id, "n": number}).fetchone()
    if row is None:
        raise not_found("Revision")
    return row["id"]


def compilation_json(latest: dict) -> dict:
    return {"id": latest["compilation_id"], "title": latest["title"], "revision_count": latest["revision_count"],
            "latest": summary_json(latest)}


# A compilation's vector: the mean of its latest revision's clips' own vectors (what each clip is about).
VECTORS_SQL = f"""
SELECT r.compilation_id, r.id AS latest_id, r.title, avg(v.embedding) AS embedding
FROM ({REVISIONS}) r
JOIN compilation_items ci ON ci.compilation_id = r.id
JOIN clip_vectors v ON v.clip_id = ci.clip_id AND v.model_name = %(model)s AND vector_norm(v.embedding) > 0
WHERE r.id = r.latest_id
GROUP BY r.compilation_id, r.id, r.title"""


def list_compilations(conn: psycopg.Connection, *, q: str | None, clip_id: int | None, limit: int,
                      cursor: str | None, ranking=None) -> tuple[list[dict], str | None]:
    """Most recently revised first, or, with a semantic `ranking`, by similarity to the query."""
    params = {"q": q, "clip": clip_id}
    clip_filter = ("EXISTS (SELECT 1 FROM compilation_items ci WHERE ci.compilation_id = {rev} AND ci.clip_id = %(clip)s)"
                   if clip_id is not None else "TRUE")
    if ranking is not None:
        params.update(model=ranking.model, min_sim=ranking.min_similarity, search="semantic")
        cut = "AND 1 - (x.embedding <=> %(qv)s::vector) >= %(min_sim)s" if ranking.min_similarity is not None else ""
        sql = f"""SELECT x.latest_id, x.compilation_id AS _id, (1 - (x.embedding <=> %(qv)s::vector))::double precision AS _k
                  FROM ({VECTORS_SQL}) x WHERE {clip_filter.format(rev='x.latest_id')} {cut}
                    AND vector_norm(x.embedding) > 0 AND vector_norm(%(qv)s::vector) > 0"""
        filters = dict(params)
        params["qv"] = ranking.vector
    else:
        where = ["r.id = r.latest_id", clip_filter.format(rev="r.id")]
        if q:  # web-search syntax over titles, like `q` everywhere else
            where.append("to_tsvector('english', r.title) @@ websearch_to_tsquery('english', %(q)s)")
        sql = f"""SELECT r.id AS latest_id, r.compilation_id AS _id, r.id::double precision AS _k
                  FROM ({REVISIONS}) r WHERE {' AND '.join(where)}"""
        filters = {"q": q, "clip": clip_id}
    page = keyset_page(conn, sql, params, descending=True, limit=limit, cursor=cursor, filters=filters)
    latest = {r["id"]: r for r in conn.execute(SUMMARY_SQL + " WHERE r.id = ANY(%s)", ([r["latest_id"] for r in page.rows],))}
    out = []
    for r in page.rows:
        item = compilation_json(latest[r["latest_id"]])
        if ranking is not None:
            item["match"] = {"similarity": float(r["_k"])}
        out.append(item)
    return out, page.next_cursor


def get_compilation(conn: psycopg.Connection, compilation_id: int) -> dict:
    return compilation_json(revision_row(conn, _compilation(conn, compilation_id)["id"]))


def list_revisions(conn: psycopg.Connection, compilation_id: int, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
    _compilation(conn, compilation_id)
    page = keyset_page(conn, f"SELECT q0.*, q0.revision::double precision AS _k, q0.id AS _id FROM ({LINEAGE_SUMMARY_SQL} WHERE r.compilation_id = %(of)s) q0",
                       {"of": compilation_id}, descending=False, limit=limit, cursor=cursor, filters={"c": compilation_id})
    return [summary_json(r) for r in page.rows], page.next_cursor


def items(conn: psycopg.Connection, revision_id: int, include: set[str]) -> list[dict]:
    rows = conn.execute(
        """SELECT ci."position", ci.clip_id, ci.transcript_version_id, ci.first_word_seq, ci.last_word_seq,
                  ci.start_ms, ci.end_ms, c.title AS clip_title, c.first_word_seq AS clip_first, c.last_word_seq AS clip_last,
                  e.id AS episode_id, e.title AS episode_title, e.published_at, p.name AS podcast,
                  e.platform, e.video_id, e.source_url,
                  (SELECT string_agg(w.word, ' ' ORDER BY w.seq) FROM transcript_words w
                    WHERE w.transcript_version_id = ci.transcript_version_id
                      AND w.seq BETWEEN ci.first_word_seq AND ci.last_word_seq) AS text
           FROM compilation_items ci
           JOIN clips c ON c.id = ci.clip_id
           JOIN runs r ON r.id = c.run_id
           JOIN episodes e ON e.id = r.episode_id
           LEFT JOIN podcasts p ON p.id = e.podcast_id
           WHERE ci.compilation_id = %s ORDER BY ci."position\"""", (revision_id,)).fetchall()
    speakers = speakers_by_episode(conn, list({r["episode_id"] for r in rows})) if "speakers" in include else {}
    captions: dict[int, list[dict]] = {}
    if "captions" in include:
        for w in conn.execute(
            """SELECT ci."position", w.word, w.start_ms - ci.start_ms AS at_ms, w.end_ms - ci.start_ms AS until_ms
               FROM compilation_items ci JOIN transcript_words w ON w.transcript_version_id = ci.transcript_version_id
                AND w.seq BETWEEN ci.first_word_seq AND ci.last_word_seq
               WHERE ci.compilation_id = %s ORDER BY ci."position", w.seq""", (revision_id,)):
            captions.setdefault(w["position"], []).append(
                {"word": w["word"], "at_ms": int(w["at_ms"]), "until_ms": int(w["until_ms"])})
    out = []
    for r in rows:
        item = {
            "position": r["position"], "clip_id": r["clip_id"], "clip_title": r["clip_title"],
            "transcript_id": r["transcript_version_id"],
            "words": {"first": r["first_word_seq"], "last": r["last_word_seq"]},
            "time": {"start_ms": r["start_ms"], "end_ms": r["end_ms"]},
            "duration_ms": r["end_ms"] - r["start_ms"], "text": r["text"] or "",
            "is_whole_clip": (r["first_word_seq"], r["last_word_seq"]) == (r["clip_first"], r["clip_last"]),
            "episode": episode_ref(r),
            "source": {"platform": r["platform"], "video_id": r["video_id"], "url": r["source_url"]},
        }
        if "speakers" in include:
            item["speakers"] = speakers.get(r["episode_id"], [])
        if "captions" in include:
            item["captions"] = captions.get(r["position"], [])
        out.append(item)
    return out


def get_revision(conn: psycopg.Connection, revision_id: int, include: set[str]) -> dict:
    row = revision_row(conn, revision_id)
    return {**summary_json(row), "is_latest": row["is_latest"], "items": items(conn, revision_id, include)}


# --- writes -----------------------------------------------------------------

def resolve_items(conn: psycopg.Connection, selections: list[dict], base: str) -> list[Resolved]:
    """Resolve every selection and run all checks; raise 422 listing every problem."""
    clips = clip_words(conn, list({s["clip_id"] for s in selections}))
    errors = ErrorList()
    resolved = [resolve(s, clips.get(s["clip_id"]), pointer(base, i), errors) for i, s in enumerate(selections)]
    check_overlaps(resolved, base, errors)
    errors.raise_if_any()
    return [r for r in resolved if r is not None]


def preview_items(conn: psycopg.Connection, resolved: list[Resolved]) -> list[dict]:
    """RevisionItem JSON for resolved-but-unsaved segments."""
    if not resolved:
        return []
    meta = {r["id"]: r for r in conn.execute(
        """SELECT c.id, c.title AS clip_title, e.id AS episode_id, e.title AS episode_title, e.published_at,
                  p.name AS podcast, e.platform, e.video_id, e.source_url
           FROM clips c JOIN runs r ON r.id = c.run_id JOIN episodes e ON e.id = r.episode_id
           LEFT JOIN podcasts p ON p.id = e.podcast_id WHERE c.id = ANY(%s)""", ([s.clip_id for s in resolved],))}
    out = []
    for i, seg in enumerate(resolved, 1):
        m = meta[seg.clip_id]
        out.append({**seg.to_json(), "position": i, "clip_title": m["clip_title"], "episode": episode_ref(m),
                    "source": {"platform": m["platform"], "video_id": m["video_id"], "url": m["source_url"]}})
    return out


def preview(conn: psycopg.Connection, title: str, number: int, based_on: int | None, metadata: dict,
            resolved: list[Resolved]) -> dict:
    return {"title": title, "number": number, "based_on": based_on, "metadata": metadata, "item_count": len(resolved),
            "duration_ms": sum(s.duration_ms for s in resolved), "items": preview_items(conn, resolved)}


def _lock_title(conn: psycopg.Connection, title: str) -> None:
    """Serialize API writers working on the same compilation (there's no lineage row to lock)."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('yolt:compilation:' || %s))", (title,))


def _insert_revision(conn: psycopg.Connection, title: str, number: int, metadata: dict,
                     resolved: list[Resolved]) -> int:
    """One compilations row, then its items."""
    revision_id = conn.execute("INSERT INTO compilations (title, revision, metadata) VALUES (%s, %s, %s) RETURNING id",
                               (title, number, Jsonb(metadata))).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO compilation_items (compilation_id, "position", clip_id, transcript_version_id,
                                              first_word_seq, last_word_seq, start_ms, end_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            [(revision_id, i, s.clip_id, s.transcript_id, s.first, s.last, s.start_ms, s.end_ms)
             for i, s in enumerate(resolved, 1)])
    return revision_id


def create_compilation(conn: psycopg.Connection, body: dict, dry_run: bool) -> tuple[int, dict]:
    title = body["title"]
    with conn.transaction():
        _lock_title(conn, title)
        resolved = resolve_items(conn, body["items"], "items")  # first, so a dry run reports item problems too
        existing = conn.execute("SELECT min(id) AS id FROM compilations WHERE title = %s", (title,)).fetchone()["id"]
        if existing is not None:
            raise ApiProblem(409, "title-taken", "A compilation with this title already exists",
                             detail="Titles identify compilations. Add a revision to the existing one instead.",
                             errors=[FieldError("title_taken", f"Compilation {existing} already has this title.",
                                                pointer="/title")],
                             extra={"code": "title_taken", "compilation_id": existing})
        if dry_run:
            return 200, preview(conn, title, 1, None, body["metadata"], resolved)
        revision_id = _insert_revision(conn, title, 1, body["metadata"], resolved)
    return 201, get_revision(conn, revision_id, set())


def create_revision(conn: psycopg.Connection, compilation_id: int, body: dict, dry_run: bool) -> tuple[int, dict]:
    with conn.transaction():
        title = _compilation(conn, compilation_id)["title"]
        _lock_title(conn, title)
        latest = _compilation(conn, compilation_id)  # re-read under the lock
        if body["based_on"] != latest["revision"]:
            raise ApiProblem(409, "revision-conflict", "The compilation has a newer revision",
                             detail=f"`based_on` is {body['based_on']} but the latest revision is {latest['revision']}.",
                             errors=[FieldError("revision_conflict", f"The latest revision is {latest['revision']}; "
                                                "re-read it and base the new revision on it.", pointer="/based_on")],
                             extra={"code": "revision_conflict", "latest_number": latest["revision"],
                                    "latest_revision_id": latest["id"]})
        resolved = resolve_items(conn, body["items"], "items")
        number = latest["revision"] + 1
        if dry_run:
            return 200, preview(conn, title, number, latest["revision"], body["metadata"], resolved)
        revision_id = _insert_revision(conn, title, number, body["metadata"], resolved)
    return 201, get_revision(conn, revision_id, set())


def diff_revisions(conn: psycopg.Connection, revision_id: int, against: int | None) -> dict:
    row = revision_row(conn, revision_id)
    if against is None:
        if row["based_on"] is None:
            raise invalid([FieldError("no_base_revision", "This is the first revision; pass `against`.",
                                      parameter="against")])
        against = revision_id_for(conn, row["compilation_id"], row["based_on"])
    else:
        revision_row(conn, against)

    def load(rid: int) -> list[diffs.Item]:
        return [diffs.Item(r["position"], r["clip_id"], r["first_word_seq"], r["last_word_seq"], r["end_ms"] - r["start_ms"])
                for r in conn.execute(
                    """SELECT "position", clip_id, first_word_seq, last_word_seq, start_ms, end_ms
                       FROM compilation_items WHERE compilation_id = %s ORDER BY "position\"""", (rid,))]

    return {"revision_id": revision_id, "against": against, **diffs.diff(load(revision_id), load(against))}
