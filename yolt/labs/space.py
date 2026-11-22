"""Every clip's vector as one matrix, for the labs that look across the whole catalogue.

A clip's vector is the one themes cluster on (yolt/repo/themes.py): the mean of its passages' vectors,
else its own clip vector; zero vectors are left out. The matrix is loaded once and kept until the
vectors' fingerprint changes. Rows are unit length, so a dot product is a cosine similarity.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np
import psycopg

from ..embeddings import DIMENSIONS, EmbeddingService
from ..repo import themes as theme_repo
from ..repo import vectors as vector_repo


@dataclass(frozen=True)
class Space:
    ids: list[int]  # clip IDs, ascending
    x: np.ndarray  # unit rows, float32
    rows: dict[int, int] = field(default_factory=dict)  # clip ID -> row

    def row(self, clip_id: int) -> int | None:
        return self.rows.get(clip_id)


_cache: dict[tuple, Space] = {}
_lock = threading.Lock()


def load(conn: psycopg.Connection, service: EmbeddingService, what: str) -> Space:
    """The current vectors. 503 `vectors-unavailable` (naming `what`) when no clip has one yet."""
    model = service.model_name
    vector_repo.require_vectors(conn, model, what)
    fp = conn.execute(theme_repo.FINGERPRINT_SQL, {"m": model}).fetchone()
    key = (model, fp["n"], fp["digest"])
    with _lock:
        if key in _cache:
            return _cache[key]
    rows = conn.execute(theme_repo.VECTORS_SQL, {"m": model}).fetchall()
    x = np.stack([theme_repo.from_binary(r["v"]) for r in rows]).astype(np.float32) if rows \
        else np.zeros((0, DIMENSIONS), np.float32)
    if len(x):
        x /= np.linalg.norm(x, axis=1, keepdims=True)
    ids = [r["clip_id"] for r in rows]
    space = Space(ids, x, {clip_id: i for i, clip_id in enumerate(ids)})
    with _lock:
        _cache.clear()  # only the current vectors are worth keeping
        _cache[key] = space
    return space

