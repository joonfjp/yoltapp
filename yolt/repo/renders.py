"""Render records, which drive `usage.rendered`."""
from __future__ import annotations

import psycopg

from ..problems import ErrorList, pointer
from .common import iso, keyset_page
from .compilations import revision_row

RENDER_SQL = """
SELECT rn.*, (SELECT min(x.id) FROM compilations x WHERE x.title = co.title) AS compilation_id, co.revision,
       COALESCE((SELECT array_agg(rp."position" ORDER BY rp."position")
                 FROM compilation_render_positions rp WHERE rp.render_id = rn.id), '{}') AS positions
FROM compilation_renders rn JOIN compilations co ON co.id = rn.revision_id"""


def render_json(r: dict) -> dict:
    return {
        "id": r["id"], "revision_id": r["revision_id"], "compilation_id": r["compilation_id"], "number": r["revision"],
        "format": r["format"], "output_path": r["output_path"], "duration_ms": r["duration_ms"],
        "spec_path": r["spec_path"], "positions": list(r["positions"]), "rendered_at": iso(r["rendered_at"]),
    }


def list_renders(conn: psycopg.Connection, revision_id: int, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
    revision_row(conn, revision_id)
    page = keyset_page(conn, f"SELECT q0.*, q0.id::double precision AS _k, q0.id AS _id FROM ({RENDER_SQL} WHERE rn.revision_id = %(r)s) q0",
                       {"r": revision_id}, descending=True, limit=limit, cursor=cursor, filters={"r": revision_id})
    return [render_json(r) for r in page.rows], page.next_cursor


def record_render(conn: psycopg.Connection, revision_id: int, body: dict) -> dict:
    with conn.transaction():
        count = revision_row(conn, revision_id)["item_count"]
        positions = body.get("positions") or list(range(1, count + 1))
        errors = ErrorList()
        for i, p in enumerate(body.get("positions") or []):
            if p > count:
                errors.add("positions_out_of_range", f"Position {p} is past the revision's last item ({count}).",
                           at=pointer("positions", i))
        errors.raise_if_any()
        render_id = conn.execute(
            """INSERT INTO compilation_renders (revision_id, format, output_path, duration_ms, spec_path)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (revision_id, body["format"], body["output_path"], body["duration_ms"], body.get("spec_path"))).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany('INSERT INTO compilation_render_positions (render_id, "position") VALUES (%s, %s)',
                            [(render_id, p) for p in sorted(positions)])
        return render_json(conn.execute(RENDER_SQL + " WHERE rn.id = %s", (render_id,)).fetchone())
