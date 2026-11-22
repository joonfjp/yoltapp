"""The embedding server: an OpenAI-compatible /v1/embeddings endpoint, configured in Settings.

Documents (clip text, passages) are embedded as-is and queries behind Qwen3's task
instruction, at 1024 dimensions. Every stored vector records `<model>@1024`, and a query
is only compared with vectors of the model it was embedded with.
"""

from __future__ import annotations

import copy
import json
import math
import threading
from collections import OrderedDict
from collections.abc import Callable

import httpx

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-8B"
DIMENSIONS = 1024  # clip_vectors.embedding and passage_vectors.embedding are vector(1024)
QUERY_INSTRUCTION = "Given a search query, retrieve podcast clips relevant to the query"
MAX_QUERY_CHARS = 300  # each new query costs a GPU call
CACHE_SIZE = 1024
QUERY_TIMEOUT = 15.0
DOCUMENT_TIMEOUT = 300.0
CONNECT_TIMEOUT = 5.0
REJECTED_STATUS = (400, 413, 422)  # the server refused these inputs, not every input


class EmbeddingUnavailable(Exception):
    """The embedding server isn't configured, reachable, or answering sensibly."""


class EmbeddingRejected(EmbeddingUnavailable):
    """The server answered, but refused these inputs (400, 413, 422) or gave unusable vectors for them (too
    few or too many, the wrong length, not finite numbers): sending the same inputs again fails the same way."""


def model_name(model: str) -> str:
    """The `model_name` stored on every vector row."""
    return f"{model}@{DIMENSIONS}"


def query_text(q: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {q}"


def literal(vector: list[float]) -> str:
    """pgvector's text form, passed as a parameter and cast with ::vector."""
    return json.dumps(vector)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class EmbeddingService:
    """Embeds with whatever Settings say now, so changing the URL or model needs no restart.

    Query embeddings are cached (per URL and model), so repeated searches and later pages
    don't call the server again. Failures aren't cached."""

    def __init__(self, settings: Callable[[], dict[str, str]], client_factory: Callable[[], httpx.Client]):
        self.settings = settings
        self.client_factory = client_factory
        self._cache: OrderedDict[tuple[str, str, str], list[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._failed: dict[tuple[str, str, str], EmbeddingUnavailable] | None = None  # pinned views only

    def pinned(self, settings: dict[str, str]) -> EmbeddingService:
        """This service with one request's Settings: it shares the query cache but never reads Settings again,
        so a request holding a pooled connection doesn't take a second one for them. A query that failed isn't
        sent again by the same view; it raises the same error."""
        view = copy.copy(self)
        view.settings = lambda: settings
        view._failed = {}
        return view

    def config(self) -> tuple[str, str]:
        s = self.settings()
        return s["embed_url"].strip(), s["embed_model"].strip() or DEFAULT_MODEL

    @property
    def configured(self) -> bool:
        return bool(self.config()[0])

    @property
    def model_name(self) -> str:
        return model_name(self.config()[1])

    def embed(self, texts: list[str], *, timeout: float = DOCUMENT_TIMEOUT,
              config: tuple[str, str] | None = None) -> list[list[float]]:
        """One DIMENSIONS-long vector per text, in order."""
        url, model = config or self.config()
        if not url:
            raise EmbeddingUnavailable("No embedding server is configured (Settings → Embeddings).")
        try:
            with self.client_factory() as client:
                # A long read timeout for big batches, but an unreachable host fails fast.
                response = client.post(url, json={"model": model, "input": texts, "dimensions": DIMENSIONS},
                                       timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT))
                response.raise_for_status()
                data = response.json()["data"]
            vectors = [row["embedding"] for row in sorted(data, key=lambda row: row["index"])]
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error = EmbeddingRejected if status in REJECTED_STATUS else EmbeddingUnavailable
            raise error(f"The embedding server failed: {type(exc).__name__} ({status}).") from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise EmbeddingUnavailable(f"The embedding server failed: {type(exc).__name__}.") from exc
        if len(vectors) != len(texts) or any(not isinstance(v, list) or len(v) != DIMENSIONS for v in vectors):
            raise EmbeddingRejected(f"The embedding server didn't return {DIMENSIONS}-dimension vectors.")
        if not all(_finite(x) for v in vectors for x in v):  # NaN can't be stored, and json.loads accepts it
            raise EmbeddingRejected("The embedding server returned a vector that isn't all finite numbers.")
        return vectors

    def query(self, q: str) -> list[float]:
        url, model = config = self.config()
        key = (url, model, q)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        if self._failed is not None and key in self._failed:
            raise self._failed[key]
        try:
            [vector] = self.embed([query_text(q)], timeout=QUERY_TIMEOUT, config=config)
        except EmbeddingUnavailable as error:
            if self._failed is not None:
                self._failed[key] = error
            raise
        with self._lock:
            self._cache[key] = vector
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return vector

    def check(self, url: str, model: str) -> str:
        """For Settings → Test: embed one word with this URL and model."""
        if not url.strip():
            raise EmbeddingUnavailable("Enter an embeddings URL.")
        self.embed(["test"], timeout=QUERY_TIMEOUT, config=(url.strip(), model.strip() or DEFAULT_MODEL))
        return f"Embeddings work ({model.strip() or DEFAULT_MODEL}, {DIMENSIONS} dimensions)."
