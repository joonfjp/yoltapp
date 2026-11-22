"""Turn segment selections into exact word ranges and times, and check them."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..problems import ErrorList, pointer
from .anchors import AnchorErrors, resolve_anchors
from .tokens import tokenize_words


@dataclass(frozen=True)
class Word:
    seq: int
    word: str
    start_ms: int
    end_ms: int


@dataclass
class ClipWords:
    """A clip's bounds plus whatever timed words exist inside them."""
    clip_id: int
    transcript_id: int
    first: int
    last: int
    words: dict[int, Word] = field(default_factory=dict)

    def in_order(self) -> list[Word]:
        return [self.words[s] for s in sorted(self.words)]


@dataclass(frozen=True)
class Resolved:
    clip_id: int
    transcript_id: int
    first: int
    last: int
    start_ms: int
    end_ms: int
    text: str
    is_whole_clip: bool

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_json(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "transcript_id": self.transcript_id,
            "words": {"first": self.first, "last": self.last},
            "time": {"start_ms": self.start_ms, "end_ms": self.end_ms},
            "duration_ms": self.duration_ms,
            "text": self.text,
            "is_whole_clip": self.is_whole_clip,
        }


def resolve(selection: dict, clip: ClipWords | None, at: str, errors: ErrorList) -> Resolved | None:
    """Resolve one selection. Adds problems to `errors` and returns None if it can't."""
    clip_id = selection["clip_id"]
    if clip is None:
        errors.add("clip_not_found", f"Clip {clip_id} does not exist.", at=at + pointer("clip_id"))
        return None
    before = len(errors.items)
    if selection.get("words") is not None:
        first, last = selection["words"]["first"], selection["words"]["last"]
        if last < first:
            errors.add("words_reversed", f"`last` ({last}) is before `first` ({first}).", at=at + pointer("words", "last"))
            return None
        if first < clip.first:
            errors.add("words_outside_clip", f"Word {first} is before the clip's first word ({clip.first}).",
                       at=at + pointer("words", "first"))
        if last > clip.last:
            errors.add("words_outside_clip", f"Word {last} is past the clip's last word ({clip.last}).",
                       at=at + pointer("words", "last"))
    elif selection.get("text") is not None:
        anchors = selection["text"]
        tokens = tokenize_words([(w.seq, w.word) for w in clip.in_order()])
        try:
            first, last = resolve_anchors(tokens, clip_id, anchors.get("from"), anchors.get("to"))
        except AnchorErrors as exc:
            for e in exc.errors:
                errors.add(e.code, e.message, at=at + pointer("text", e.field))
            return None
    else:
        first, last = clip.first, clip.last
    if len(errors.items) > before:
        return None
    missing = [s for s in range(first, last + 1) if s not in clip.words]
    if missing:
        shown = ", ".join(map(str, missing[:5])) + (" …" if len(missing) > 5 else "")
        errors.add("words_missing", f"{len(missing)} word(s) have no timing in transcript {clip.transcript_id}: {shown}.",
                   at=at + (pointer("words") if selection.get("words") is not None else pointer("clip_id")))
        return None
    span = [clip.words[s] for s in range(first, last + 1)]
    if span[-1].end_ms <= span[0].start_ms:
        errors.add("invalid_value", f"Words {first}–{last} have no duration.", at=at)
        return None
    return Resolved(
        clip_id=clip_id,
        transcript_id=clip.transcript_id,
        first=first,
        last=last,
        start_ms=span[0].start_ms,
        end_ms=span[-1].end_ms,
        text=" ".join(w.word for w in span),
        is_whole_clip=(first, last) == (clip.first, clip.last),
    )


def check_overlaps(resolved: list[Resolved | None], base: str, errors: ErrorList) -> None:
    """No word may play twice: items from the same transcript must not overlap, whatever clip they came from."""
    seen: list[tuple[int, Resolved]] = []
    for i, seg in enumerate(resolved):
        if seg is None:
            continue
        for j, prev in seen:
            if prev.transcript_id == seg.transcript_id and prev.first <= seg.last and seg.first <= prev.last:
                errors.add("overlapping_segments",
                           f"Item {i + 1} (words {seg.first}–{seg.last}) overlaps item {j + 1} "
                           f"(words {prev.first}–{prev.last}) in transcript {seg.transcript_id}.",
                           at=pointer(base, i))
                break
        seen.append((i, seg))

