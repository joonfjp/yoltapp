"""Validate real responses against spec/yolt.openapi.json.

Every /api/v1 response the test client sees goes through `check`, so the spec and
the server can't drift apart silently. `COVERAGE` records which (operation, status)
pairs the suite exercised; a full run fails if any operation went untested.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import cache

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from yolt.server import SPEC_PATH

SPEC = json.loads(SPEC_PATH.read_text())
PREFIX = "/api/v1"
REGISTRY = Registry().with_resource("spec", Resource.from_contents(SPEC, default_specification=DRAFT202012))
COVERAGE: dict[str, set[str]] = defaultdict(set)


class ContractError(AssertionError):
    pass


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


@cache
def _routes() -> list[tuple[re.Pattern, str, str]]:
    out = []
    for path, item in SPEC["paths"].items():
        full = PREFIX + (path.rstrip("/") if path != "/" else "/")
        regex = "^" + re.sub(r"\\\{[^}]+\\\}", r"[^/]+", re.escape(full)) + "$"
        for method in item:
            out.append((re.compile(regex), method, path))
    return sorted(out, key=lambda r: r[2].count("{"))  # literal paths before templated ones


def operation_for(method: str, url_path: str) -> tuple[str, str] | None:
    for regex, m, path in _routes():
        if m == method.lower() and regex.match(url_path):
            return path, SPEC["paths"][path][m]["operationId"]
    return None


def _resolve(node: dict, base: str) -> tuple[dict, str]:
    while "$ref" in node:
        ref = node["$ref"]
        base = "spec" + ref
        node = SPEC
        for part in ref.lstrip("#/").split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
    return node, base


def check(method: str, url_path: str, status: int, content_type: str | None, body: bytes) -> str | None:
    """Raise ContractError unless the response matches the spec. Returns the operationId."""
    if not url_path.startswith(PREFIX):
        return None
    found = operation_for(method, url_path)
    if found is None:
        raise ContractError(f"{method} {url_path} is not in the spec")
    path, op_id = found
    op = SPEC["paths"][path][method.lower()]
    key = str(status) if str(status) in op["responses"] else "default"
    if key == "default" and status < 500:
        raise ContractError(f"{op_id}: status {status} is not documented")
    COVERAGE[op_id].add(str(status))
    base = f"spec#/paths/{_escape(path)}/{method.lower()}/responses/{key}"
    response, base = _resolve(op["responses"][key], base)
    content = response.get("content")
    if not content:
        if body:
            raise ContractError(f"{op_id} {status}: spec documents no body but got {body[:80]!r}")
        return op_id
    media = (content_type or "").split(";")[0].strip()
    match = next((m for m in content if m.split(";")[0].strip() == media), None)
    if match is None:
        raise ContractError(f"{op_id} {status}: content type {content_type!r} not in {list(content)}")
    if not media.endswith("json"):
        return op_id
    validator = Draft202012Validator({"$ref": f"{base}/content/{_escape(match)}/schema"}, registry=REGISTRY)
    errors = sorted(validator.iter_errors(json.loads(body)), key=lambda e: list(e.absolute_path))
    if errors:
        detail = "\n".join(f"  at /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors[:10])
        raise ContractError(f"{op_id} {status} does not match the spec:\n{detail}")
    return op_id


def all_operations() -> set[str]:
    return {op["operationId"] for item in SPEC["paths"].values() for op in item.values()}


class ContractClient:
    """A TestClient wrapper that validates every API response against the spec."""

    def __init__(self, client):
        self.client = client
        self.app = client.app

    def request(self, method: str, url: str, **kw):
        resp = self.client.request(method, url, follow_redirects=False, **kw)
        check(method, resp.request.url.path, resp.status_code, resp.headers.get("content-type"), resp.content)
        return resp

    def get(self, url: str, **kw):
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw):
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw):
        return self.request("PUT", url, **kw)

    def patch(self, url: str, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url: str, **kw):
        return self.request("DELETE", url, **kw)
