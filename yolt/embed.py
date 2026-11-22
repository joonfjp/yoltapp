"""Embed clips and passages that have no vector from the current model, or whose text changed.

The worker does this after every run and, when idle, catches up on anything left over (the
embedding server was down, or the model changed). To do it all now:

    uv run python -m yolt.embed        # or: make embed
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .db import Database
from .embeddings import EmbeddingRejected, EmbeddingService, EmbeddingUnavailable, literal, model_name

log = logging.getLogger("yolt.embed")
BATCH_SIZE = 32

# A clip is embedded as its title, summary and quotes; the digest is sha256 of that text.
CLIP_TEXT = """SELECT c.id, concat_ws(E'\\n', c.title, c.summary,
                      string_agg(q.quote_text, E'\\n' ORDER BY q.ordinal)) AS text
               FROM clips c LEFT JOIN clip_quotes q ON q.clip_id = c.id {where} GROUP BY c.id"""
DIGEST = "encode(sha256(convert_to({text}, 'UTF8')), 'hex')"
# Clips never change once stored, so a clip is pending only when it has no vector from this model; that
# check is a cheap anti-join, and only the pending clips' text is built and hashed.
PENDING_CLIPS = f"""WITH todo AS (
    SELECT c.id FROM clips c LEFT JOIN clip_vectors v ON v.clip_id = c.id
    WHERE (v.clip_id IS NULL OR v.model_name <> %(m)s) AND c.id <> ALL(%(skip)s::bigint[]) {{run}}
    ORDER BY c.id LIMIT %(limit)s)
SELECT t.id, t.text, {DIGEST.format(text="t.text")} AS digest
FROM ({CLIP_TEXT.format(where="WHERE c.id IN (SELECT id FROM todo)")}) t ORDER BY t.id"""
PENDING_PASSAGES = """SELECT p.id, p.text, p.text_digest AS digest
FROM passages p LEFT JOIN passage_vectors v ON v.passage_id = p.id
WHERE (v.passage_id IS NULL OR v.model_name <> %(m)s OR v.text_digest <> p.text_digest)
    AND p.id <> ALL(%(skip)s::bigint[]) {where}
ORDER BY p.id LIMIT %(limit)s"""
# A clip `c` is searchable by meaning once it and all its passages have vectors from model %(m)s.
EMBEDDED_CLIP = """(EXISTS (SELECT 1 FROM clip_vectors v WHERE v.clip_id = c.id AND v.model_name = %(m)s)
    AND NOT EXISTS (SELECT 1 FROM passages p LEFT JOIN passage_vectors pv
                      ON pv.passage_id = p.id AND pv.model_name = %(m)s AND pv.text_digest = p.text_digest
                    WHERE p.clip_id = c.id AND pv.passage_id IS NULL))"""
UPSERT = """INSERT INTO {table} ({key}, model_name, text_digest, embedding) VALUES (%s, %s, %s, %s::vector)
ON CONFLICT ({key}) DO UPDATE SET model_name = excluded.model_name, text_digest = excluded.text_digest,
    embedding = excluded.embedding, embedded_at = now()"""

# Inputs the embedding server rejected on their own (EmbeddingRejected), by (url, model, table, id), with
# why. They're skipped so they can't hold up everything after them. Nothing in the database records them,
# so a restart (or new embedding settings) tries them again.
_rejected: dict[tuple[str, str, str, int], str] = {}


@dataclass
class Embedded:
    clips: int = 0
    passages: int = 0

    @property
    def total(self) -> int:
        return self.clips + self.passages


def embed_pending(db: Database, service: EmbeddingService, *, run_id: int | None = None, limit: int | None = None,
                  on_batch: Callable[[str, int], None] = lambda kind, n: None) -> Embedded:
    """Embed up to `limit` pending clips and passages (of one run, or everywhere), a batch at a time.

    Raises EmbeddingUnavailable when the server can't help; batches already stored stay stored. Each
    attempt is recorded (Database.record_embedding), so GET / and the UI can show a failure."""
    config = service.config()  # read once: the vectors are labeled with the model that made them
    for key in [key for key in _rejected if key[:2] != config]:  # other settings: tried again if they're back
        del _rejected[key]
    try:
        done = _embed_pending(db, service, config, run_id=run_id, limit=limit, on_batch=on_batch)
    except Exception as error:
        _record(db, str(error) or type(error).__name__)
        raise
    _record(db, _rejections(config))
    return done


def _skipped(config: tuple[str, str], table: str) -> list[int]:
    return [item for (url, model, kind, item) in _rejected if (url, model) == config and kind == table]


def _rejections(config: tuple[str, str]) -> str | None:
    """The error to show while inputs are being skipped, or None."""
    found = [(kind, item, why) for (url, model, kind, item), why in _rejected.items() if (url, model) == config]
    if not found:
        return None
    kind, item, why = found[0]
    return (f"The embedding server rejected {len(found)} item(s) on their own (first: {kind} {item}: {why}); "
            "they're skipped until Yolt restarts or the embedding settings change.")


def _record(db: Database, error: str | None) -> None:
    try:
        db.record_embedding(error)
    except Exception:  # noqa: BLE001 - e.g. the database is what failed; the caller reports the error itself
        log.exception("couldn't record the embedding attempt")


def _embed_pending(db: Database, service: EmbeddingService, config: tuple[str, str], *, run_id: int | None,
                   limit: int | None, on_batch: Callable[[str, int], None]) -> Embedded:
    if not config[0]:
        raise EmbeddingUnavailable("No embedding server is configured (Settings → Embeddings).")
    model = model_name(config[1])
    params = {"m": model, "run": run_id, "limit": limit, "skip": _skipped(config, "clip_vectors")}
    clip_run = "AND c.run_id = %(run)s" if run_id is not None else ""
    passage_where = ("AND p.clip_id IN (SELECT id FROM clips WHERE run_id = %(run)s)" if run_id is not None else "")
    done = Embedded()
    with db.connect() as conn:
        clips = conn.execute(PENDING_CLIPS.format(run=clip_run), params).fetchall()
    done.clips = _store(db, service, config, "clip_vectors", "clip_id", clips, model, on_batch)
    if limit is not None:
        params["limit"] = max(0, limit - len(clips))  # rejected ones count: they were tried
    params["skip"] = _skipped(config, "passage_vectors")
    with db.connect() as conn:
        passages = conn.execute(PENDING_PASSAGES.format(where=passage_where), params).fetchall()
    done.passages = _store(db, service, config, "passage_vectors", "passage_id", passages, model, on_batch)
    return done


def _store(db, service, config, table, key, rows, model, on_batch) -> int:
    sql = UPSERT.format(table=table, key=key)
    stored = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        try:
            pairs = list(zip(batch, service.embed([r["text"] for r in batch], config=config), strict=True))
        except EmbeddingRejected:
            pairs = _one_by_one(service, config, table, batch)
        if pairs:
            with db.connect() as conn, conn.cursor() as cur:
                cur.executemany(sql, [(r["id"], model, r["digest"], literal(v)) for r, v in pairs])
        stored += len(pairs)
        on_batch(table, stored)
    return stored


def _one_by_one(service, config, table, batch) -> list[tuple[dict, list[float]]]:
    """A rejected batch, an input at a time, so the ones the server takes are stored and the others skipped.
    An outage meanwhile still raises. A batch rejected in full is likelier the server's settings than its
    inputs: those are skipped too (or they'd be tried first forever), but this attempt fails, so it backs off."""
    pairs, refused = [], {}
    for r in batch:
        try:
            [vector] = service.embed([r["text"]], config=config)
        except EmbeddingRejected as error:
            refused[(*config, table, r["id"])] = str(error)
            continue
        pairs.append((r, vector))
    if not pairs and len(batch) > 1:
        # Nothing got through: likelier the server's settings than these inputs, so none is skipped (or a
        # misconfigured server would have the whole catalogue skipped until a restart). The attempt fails, so
        # it backs off and tries the same inputs again later.
        raise EmbeddingUnavailable(f"The embedding server rejected all {len(batch)} inputs of a batch, one at a "
                                   "time too; check its model and settings.")
    for key, why in refused.items():
        log.warning("%s %s: the embedding server rejected it (%s); skipping it", table, key[-1], why)
    _rejected.update(refused)
    return pairs


def main(client_factory: Callable[[], httpx.Client] = httpx.Client) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    url = os.environ.get("YOLT_DATABASE_URL")
    if not url:
        print("Set YOLT_DATABASE_URL (make db writes it to .env).", file=sys.stderr)
        return 2
    db = Database(url)
    try:
        service = EmbeddingService(db.settings, client_factory)
        print(f"Embedding with {service.model_name} at {service.config()[0] or '(not configured)'}")
        done = embed_pending(db, service, on_batch=lambda table, n: print(f"  {table}: {n}", flush=True))
        print(f"Done: {done.clips} clips, {done.passages} passages embedded.")
        return 0
    except EmbeddingUnavailable as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
