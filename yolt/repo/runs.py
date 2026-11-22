"""Runs: clipping jobs on a video, their status, and the clips they produced."""

from __future__ import annotations

import psycopg

from ..db import RUN_SELECT
from ..embed import EMBEDDED_CLIP
from ..problems import not_found
from . import clips as clips_repo
from .common import episode_info, iso, keyset_page

RUN_SQL = RUN_SELECT  # one definition, shared with the worker


def run_json(r: dict, embedded_clip_count: int) -> dict:
    return {
        "id": r["id"], "episode": episode_info(r), "instructions": r["instructions"], "options": r["options"],
        "status": r["status"],
        "stage": r["stage"], "stage_started_at": iso(r["stage_started_at"]), "step": r["step"],
        "step_started_at": iso(r["step_started_at"]), "progress": r["progress"],
        "detail": r["detail"], "error": r["error"], "attempts": r["attempts"],
        "transcript_id": r["transcript_version_id"], "transcript_source": r["transcript_source"],
        "clip_count": r["clip_count"], "embedded_clip_count": embedded_clip_count,
        "created_at": iso(r["created_at"]), "finished_at": iso(r["finished_at"]),
    }


def load_runs(conn: psycopg.Connection, ids: list[int], model: str) -> list[dict]:
    """Full Run objects, in the order of `ids`. `model` is the embedding model clips are counted against."""
    if not ids:
        return []
    rows = {r["id"]: r for r in conn.execute(f"{RUN_SQL} WHERE r.id = ANY(%s)", (ids,))}
    embedded = {r["run_id"]: r["n"] for r in conn.execute(
        f"SELECT c.run_id, count(*) AS n FROM clips c WHERE c.run_id = ANY(%(ids)s) AND {EMBEDDED_CLIP} "
        "GROUP BY c.run_id", {"ids": ids, "m": model})}
    return [run_json(rows[i], embedded.get(i, 0)) for i in ids if i in rows]


def get_run(conn: psycopg.Connection, run_id: int, include: set[str], base: str, model: str) -> dict:
    found = load_runs(conn, [run_id], model)
    if not found:
        raise not_found("Run")
    run = found[0]
    if "clips" in include:
        run["clips"] = clips_repo.load_clips(conn, clips_repo.run_clip_ids(conn, run_id), set(), base)
    return run


def runs_for_video(conn: psycopg.Connection, platform: str, video_id: str, model: str) -> list[dict]:
    """Newest first."""
    ids = [r["id"] for r in conn.execute(
        """SELECT r.id FROM runs r JOIN episodes e ON e.id = r.episode_id
           WHERE e.platform = %s AND e.video_id = %s ORDER BY r.created_at DESC, r.id DESC""", (platform, video_id))]
    return load_runs(conn, ids, model)


def list_runs(conn: psycopg.Connection, *, episode_ids: list[int], statuses: list[str], limit: int,
              cursor: str | None, model: str) -> tuple[list[dict], str | None]:
    params = {"episodes": episode_ids, "statuses": statuses}
    where = ["TRUE"]
    if episode_ids:
        where.append("r.episode_id = ANY(%(episodes)s)")
    if statuses:
        where.append("r.status = ANY(%(statuses)s)")
    sql = f"""SELECT r.id AS _id, extract(epoch FROM r.created_at)::double precision AS _k
              FROM runs r WHERE {' AND '.join(where)}"""
    page = keyset_page(conn, sql, params, descending=True, limit=limit, cursor=cursor, filters=params)
    return load_runs(conn, [r["_id"] for r in page.rows], model), page.next_cursor


def queue(conn: psycopg.Connection, model: str) -> dict:
    """What the worker is doing: the run(s) processing, in full, and the queued runs in the order they'll
    run (as claim_next_run picks them), by id only, so polling it stays cheap however long the queue."""
    processing = [r["id"] for r in conn.execute(
        "SELECT id FROM runs WHERE status = 'processing' ORDER BY queued_at, id")]
    queued = conn.execute("SELECT id, episode_id FROM runs WHERE status = 'queued' ORDER BY queued_at, id").fetchall()
    return {"processing": load_runs(conn, processing, model),
            "queued": [{"id": r["id"], "episode_id": r["episode_id"]} for r in queued]}


def totals(conn: psycopg.Connection) -> dict:
    """What the UI's sidebar counts: the channels subscribed to, and the episodes with a run."""
    return conn.execute("""SELECT (SELECT count(*) FROM subscriptions) AS subscriptions,
                                  (SELECT count(DISTINCT episode_id) FROM runs) AS episodes""").fetchone()


def queue_counts(conn: psycopg.Connection) -> dict:
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, count(*) AS n FROM runs WHERE status IN ('queued', 'processing') GROUP BY status")}
    return {"queued": counts.get("queued", 0), "processing": counts.get("processing", 0)}
