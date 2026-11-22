"""RFC 9457 problem details."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROBLEM_TYPE = "https://yolt.local/problems/"


@dataclass(frozen=True)
class FieldError:
    code: str
    message: str
    pointer: str | None = None
    parameter: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.pointer is not None:
            out["pointer"] = self.pointer
        else:
            out["parameter"] = self.parameter
        return out


class ApiProblem(Exception):
    def __init__(self, status: int, slug: str, title: str, *, detail: str | None = None,
                 errors: list[FieldError] | None = None, extra: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(title)
        self.status, self.slug, self.title, self.detail = status, slug, title, detail
        self.errors = errors or []
        self.extra = extra or {}
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": PROBLEM_TYPE + self.slug, "title": self.title, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if self.errors:
            out["errors"] = [e.to_json() for e in self.errors]
        out.update(self.extra)
        return out


def invalid(errors: list[FieldError], title: str = "The request is invalid") -> ApiProblem:
    return ApiProblem(422, "invalid-request", title, errors=errors)


def bad_request(errors: list[FieldError], title: str = "Bad request") -> ApiProblem:
    return ApiProblem(400, "bad-request", title, errors=errors)


def not_found(what: str) -> ApiProblem:
    return ApiProblem(404, "not-found", f"{what} not found")


def unstorable(text: str) -> bool:
    """Whether `text` can't be stored or sent: PostgreSQL text and jsonb refuse NUL, and an unpaired
    surrogate (JSON can spell one, "\\ud800") has no UTF-8 encoding."""
    if "\x00" in text:
        return True
    try:
        text.encode()
    except UnicodeEncodeError:
        return True
    return False


def pointer(*parts: Any) -> str:
    """JSON Pointer from path parts, escaping ~ and / per RFC 6901."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


@dataclass
class ErrorList:
    """Collects every problem before failing, so clients can fix a request in one pass."""
    items: list[FieldError] = field(default_factory=list)

    def add(self, code: str, message: str, *, at: str | None = None, parameter: str | None = None) -> None:
        self.items.append(FieldError(code, message, pointer=at, parameter=parameter))

    def raise_if_any(self, status: int = 422) -> None:
        if self.items:
            raise invalid(self.items) if status == 422 else bad_request(self.items)
