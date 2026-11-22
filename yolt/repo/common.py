"""Shared query helpers: keyset pagination, row mappers, and the links a clip carries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from .. import youtube
from ..domain import cursor as cursors
from ..problems import FieldError, bad_request, unstorable

# One latest revision per compilation: the highest revision number (titles are unique per number).
LATEST_SQL = """SELECT DISTINCT ON (title) id, title, revision FROM compilations ORDER BY title, revision DESC, id DESC"""

# Per clip: compilations whose latest revision uses it, revisions that use it, and recorded renders including it.
CLIP_USAGE_SQL = f"""
SELECT ci.clip_id,
       count(DISTINCT co.title) FILTER (WHERE l.id IS NOT NULL) AS compilations,
       count(DISTINCT co.id) AS revisions,
       count(DISTINCT rn.id) FILTER (WHERE rp.render_id IS NOT NULL) AS rendered,
       max(rn.rendered_at) FILTER (WHERE rp.render_id IS NOT NULL) AS last_rendered_at
FROM compilation_items ci
JOIN compilations co ON co.id = ci.compilation_id
LEFT JOIN ({LATEST_SQL}) l ON l.id = co.id
LEFT JOIN compilation_renders rn ON rn.revision_id = ci.compilation_id
LEFT JOIN compilation_render_positions rp ON rp.render_id = rn.id AND rp."position" = ci."position"
GROUP BY ci.clip_id"""


@dataclass(frozen=True)
class Page:
    rows: list[dict]
    next_cursor: str | None


BIGINT_MIN, BIGINT_MAX = -2 ** 63, 2 ** 63 - 1


def _cursor_key(cursor: str, filters: dict[str, Any], text_key: bool) -> tuple[float | str, int]:
    """The (sort key, id) a cursor resumes after. The fingerprint binds a cursor to its filters but doesn't
    protect the key, so an edited one is checked too: a finite number (or text, with `text_key`) and an id in
    bigint range."""
    try:
        key = cursors.decode(cursor, filters)
    except cursors.CursorError as exc:
        raise bad_request([FieldError("invalid_value", str(exc), parameter="cursor")]) from exc
    if len(key) == 2 and type(key[1]) is int and BIGINT_MIN <= key[1] <= BIGINT_MAX:  # type(): bool isn't an id
        sort_key, last_id = key
        if text_key and isinstance(sort_key, str) and not unstorable(sort_key):
            return sort_key, last_id
        if not text_key and type(sort_key) in (int, float):
            try:
                number = float(sort_key)
            except OverflowError:  # an int past float range
                number = math.nan
            if math.isfinite(number):
                return number, last_id
    raise bad_request([FieldError("invalid_value", "The cursor is malformed.", parameter="cursor")])


def keyset_page(conn: psycopg.Connection, base_sql: str, params: dict[str, Any], *, descending: bool,
                limit: int, cursor: str | None, filters: dict[str, Any], text_key: bool = False) -> Page:
    """Page through `base_sql`, which must select a `_k` sort key (numeric, or text with `text_key`) and a unique
    bigint `_id` tiebreak."""
    params = dict(params)
    where = ""
    if cursor is not None:
        params["_ck"], params["_ci"] = _cursor_key(cursor, filters, text_key)
        key_type = "text" if text_key else "double precision"
        where = f"WHERE (q._k, q._id) {'<' if descending else '>'} (%(_ck)s::{key_type}, %(_ci)s::bigint)"
    order = "DESC" if descending else "ASC"
    params["_limit"] = limit + 1
    rows = conn.execute(
        f"SELECT * FROM ({base_sql}) q {where} ORDER BY q._k {order}, q._id {order} LIMIT %(_limit)s", params
    ).fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = cursors.encode([last["_k"] if text_key else float(last["_k"]), int(last["_id"])], filters)
    return Page(rows, next_cursor)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def day(value: datetime | None) -> str | None:
    return value.date().isoformat() if value is not None else None


def episode_ref(row: dict, prefix: str = "episode_") -> dict:
    return {
        "id": row[prefix + "id"],
        "title": row[prefix + "title"],
        "podcast": row.get("podcast"),
        "published_at": day(row.get("published_at")),
    }


def episode_info(row: dict, prefix: str = "episode_") -> dict:
    """EpisodeRef plus where the video lives and how long it is."""
    return {
        **episode_ref(row, prefix),
        "podcast_url": row.get("podcast_url"),
        "source": {"platform": row["platform"], "video_id": row["video_id"], "url": row["source_url"]},
        "duration_ms": row["duration_ms"],
    }


def speakers_by_episode(conn: psycopg.Connection, episode_ids: list[int]) -> dict[int, list[dict]]:
    """Each episode's speakers: hosts first, then by name."""
    out: dict[int, list[dict]] = {e: [] for e in episode_ids}
    if not episode_ids:
        return out
    for r in conn.execute(
        """SELECT es.episode_id, s.id, s.name, es.role, es.verified, es.attribution_method
           FROM episode_speakers es JOIN speakers s ON s.id = es.speaker_id
           WHERE es.episode_id = ANY(%s) ORDER BY es.role = 'guest', s.name, s.id""", (episode_ids,)):
        out[r["episode_id"]].append({"id": r["id"], "name": r["name"], "role": r["role"], "verified": r["verified"],
                                     "attribution_method": r["attribution_method"]})
    return out


def clip_links(base: str, row: dict) -> dict:
    """share_url, thumbnail_url and links for a clip row with slug, has_thumb, video_id and its times.

    `base` is the URL the request came in on, so links work from wherever the API is reached."""
    video, start, end = row["video_id"], row["start_ms"], row["end_ms"]
    video_thumbnail = youtube.thumbnail_url(video)
    return {
        "share_url": f"{base}c/{row['slug']}",
        "thumbnail_url": f"{base}media/thumbs/{row['slug']}.jpg" if row["has_thumb"] else video_thumbnail,
        "links": {
            "watch": f"{youtube.watch_url(video)}&t={start // 1000}s",
            "embed": (f"https://www.youtube.com/embed/{video}"
                      f"?start={start // 1000}&end={math.ceil(end / 1000)}&rel=0&playsinline=1"),
            "video_thumbnail": video_thumbnail,
        },
    }
