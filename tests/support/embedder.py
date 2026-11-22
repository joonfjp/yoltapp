"""A deterministic stand-in for the embedding server, so no test calls a GPU.

Texts are bags of (Unicode) words hashed into 1024 buckets, so similarity tracks shared words.
Like Qwen3, a query's instruction isn't part of its meaning: only the text after
"Query: " is embedded.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

import httpx

from yolt.embeddings import DIMENSIONS


def vector(text: str) -> list[float]:
    out = [0.0] * DIMENSIONS
    for word in re.findall(r"\w+", text.split("\nQuery: ", 1)[-1].lower()):
        out[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]


class FakeEmbeddingServer:
    """Answers POST */v1/embeddings like an OpenAI-compatible server."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.down = False
        self.rejects: set[str] = set()  # a batch with an input containing one of these words is a 400
        self.nans: set[str] = set()  # an input containing one of these words embeds as NaNs

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/embeddings"):
            return httpx.Response(404)
        if self.down:
            return httpx.Response(503, text="GPU busy")
        body = json.loads(request.content)
        assert body["dimensions"] == DIMENSIONS
        self.calls.append(body["input"])
        if any(self._has(text, self.rejects) for text in body["input"]):
            return httpx.Response(400, json={"error": "input too long"})
        answer = {"model": body["model"], "data": [
            {"index": i, "embedding": [math.nan] * DIMENSIONS if self._has(text, self.nans) else vector(text)}
            for i, text in enumerate(body["input"])]}
        # json.dumps writes NaN as servers do (httpx's json= refuses it); json.loads reads it back.
        return httpx.Response(200, content=json.dumps(answer), headers={"content-type": "application/json"})

    @staticmethod
    def _has(text: str, words: set[str]) -> bool:
        return bool(words & set(re.findall(r"\w+", text.lower())))

    def client_factory(self):
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler))

    @property
    def queries(self) -> list[str]:
        return [t for batch in self.calls for t in batch if t.startswith("Instruct: ")]
