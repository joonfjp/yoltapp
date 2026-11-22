"""HTTP byte ranges over UTF-8 text, widened so a character is never split."""
from __future__ import annotations

import re

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeError(ValueError):
    """Syntactically valid but unsatisfiable (416)."""


def parse(header: str, size: int) -> tuple[int, int] | None:
    """Inclusive (start, end) byte offsets, or None to send the whole body.

    Malformed, reversed or multi-range headers are ignored (RFC 9110 lets a server do that)."""
    m = _RANGE.match(header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    a, b = m.group(1), m.group(2)
    if a == "":  # suffix range: the last N bytes
        n = int(b)
        if n == 0:
            raise RangeError("Empty suffix range.")
        return max(0, size - n), size - 1
    start = int(a)
    if b != "" and int(b) < start:
        return None
    end = size - 1 if b == "" else min(int(b), size - 1)
    if start >= size:
        raise RangeError(f"Range starts at {start} but the text is {size} bytes.")
    return start, end


def _continuation(byte: int) -> bool:
    return byte & 0b1100_0000 == 0b1000_0000


def widen(data: bytes, start: int, end: int) -> tuple[int, int]:
    """Move start back and end forward to UTF-8 character boundaries."""
    while start > 0 and _continuation(data[start]):
        start -= 1
    while end + 1 < len(data) and _continuation(data[end + 1]):
        end += 1
    return start, end
