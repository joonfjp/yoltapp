"""Opaque keyset cursors bound to the filters and sort that produced them."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any


class CursorError(ValueError):
    pass


def fingerprint(filters: dict[str, Any]) -> str:
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode(key: list[Any], filters: dict[str, Any]) -> str:
    raw = json.dumps({"k": key, "f": fingerprint(filters)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode(cursor: str, filters: dict[str, Any]) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        key, fp = data["k"], data["f"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CursorError("The cursor is malformed.") from exc
    if not isinstance(key, list) or fp != fingerprint(filters):
        raise CursorError("The cursor was produced by a request with different filters or sort.")
    return key
