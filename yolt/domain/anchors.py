"""Resolve text anchors ({from, to}) to an inclusive word range inside a clip."""
from __future__ import annotations

from .tokens import Token, normalize


class AnchorError(Exception):
    def __init__(self, field: str, code: str, message: str):
        super().__init__(message)
        self.field = field  # "from" or "to"
        self.code = code
        self.message = message


def _find(tokens: list[Token], phrase: list[str], start: int = 0) -> list[int]:
    n = len(phrase)
    return [i for i in range(start, len(tokens) - n + 1)
            if all(tokens[i + k].norm == phrase[k] for k in range(n))]


def _phrase(raw: str, field: str) -> list[str]:
    phrase = normalize(raw)
    if not phrase:
        raise AnchorError(field, "invalid_value", f"{field!r} has no words after removing punctuation.")
    return phrase


def resolve_anchors(tokens: list[Token], clip_id: int, frm: str | None, to: str | None) -> tuple[int, int]:
    """Return (first_seq, last_seq). Raises AnchorError(s) — every problem, not just the first."""
    if not tokens:
        raise AnchorErrors([AnchorError("from" if frm is not None else "to", "words_missing",
                                        f"Clip {clip_id} has no timed words.")])
    errors: list[AnchorError] = []
    first_seq, search_from = tokens[0].seq, 0
    if frm is not None:
        try:
            phrase = _phrase(frm, "from")
            hits = _find(tokens, phrase)
            if not hits:
                raise AnchorError("from", "text_not_found", f"{frm!r} does not occur in clip {clip_id}.")
            if len(hits) > 1:
                raise AnchorError("from", "text_ambiguous",
                                  f"{frm!r} occurs {len(hits)} times in clip {clip_id}; add more words.")
            first_seq, search_from = tokens[hits[0]].seq, hits[0] + len(phrase)
        except AnchorError as exc:
            errors.append(exc)
    last_seq = tokens[-1].seq
    if to is not None:
        try:
            phrase = _phrase(to, "to")
            if errors:  # `to` is searched relative to `from`; without a valid `from` only check it exists.
                if not _find(tokens, phrase):
                    raise AnchorError("to", "text_not_found", f"{to!r} does not occur in clip {clip_id}.")
            else:
                hits = _find(tokens, phrase, search_from)
                if not hits:
                    if frm is not None and _find(tokens, phrase):
                        raise AnchorError("to", "text_out_of_order",
                                          f"{to!r} occurs in clip {clip_id} only before the end of {frm!r}.")
                    raise AnchorError("to", "text_not_found", f"{to!r} does not occur in clip {clip_id}"
                                      + (f" after {frm!r}." if frm is not None else "."))
                if len(hits) > 1:
                    where = f" after {frm!r}" if frm is not None else ""
                    raise AnchorError("to", "text_ambiguous",
                                      f"{to!r} occurs {len(hits)} times in clip {clip_id}{where}; add more words.")
                last_seq = tokens[hits[0] + len(phrase) - 1].seq
        except AnchorError as exc:
            errors.append(exc)
    if errors:
        raise AnchorErrors(errors)
    return first_seq, last_seq


class AnchorErrors(Exception):
    def __init__(self, errors: list[AnchorError]):
        super().__init__("; ".join(e.message for e in errors))
        self.errors = errors
