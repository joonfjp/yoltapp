"""Turn a word-timed transcript and Highlightr's clips into catalogue rows.

Pure functions; db.py does the writes. Words are numbered from 1 (`seq`) with
integer-millisecond times, the shape the catalogue stores and serves.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field

from .domain.segments import Word  # one Word type for ingest, storage and segments

log = logging.getLogger("yolt.ingest")

PASSAGE_VERSION = "20s-v1"
PASSAGE_MS = 20_000
PUNCTUATED_MIN = 0.01  # sentence ends per word; YouTube auto-captions have about 0.001, real text 0.05+
SENTENCE_END = re.compile(r"[.?!][\"'”’)\]]*$")


@dataclass(frozen=True)
class Quote:
    ordinal: int
    text: str
    first_word_seq: int | None
    last_word_seq: int | None


@dataclass(frozen=True)
class Passage:
    first_word_seq: int
    last_word_seq: int
    text: str
    text_digest: str


@dataclass
class NewClip:
    slug: str
    local_clip_id: str | None
    rank: int
    title: str
    summary: str | None
    start_ms: int
    end_ms: int
    first_word_seq: int
    last_word_seq: int
    overall_score: float | None
    details: dict
    quotes: list[Quote] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    has_thumb: bool = False


def new_slug() -> str:
    """Short, unguessable id used in share links."""
    return secrets.token_urlsafe(6)


def ms(seconds: float) -> int:
    return round(float(seconds) * 1000)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- transcripts -----------------------------------------------------------------


def word_rows(words: list[dict]) -> list[Word]:
    """Number `[{text, start_seconds, end_seconds}]` from 1, in milliseconds."""
    out = []
    for seq, w in enumerate(words, 1):
        start = max(0, ms(w["start_seconds"]))
        out.append(Word(seq, w["text"], start, max(start, ms(w["end_seconds"]))))
    return out


def as_seconds(words: list[Word]) -> list[dict]:
    """Back to the `[{text, start_seconds, end_seconds}]` shape transcript.py works in."""
    return [{"text": w.word, "start_seconds": w.start_ms / 1000, "end_seconds": w.end_ms / 1000} for w in words]


def timing_digest(words: list[Word]) -> str:
    """Identifies a transcript by its exact words and timings."""
    return sha256(json.dumps([[w.word, w.start_ms, w.end_ms] for w in words],
                             ensure_ascii=False, separators=(",", ":")))


def full_text(words: list[Word]) -> str:
    return " ".join(w.word for w in words)


def punctuated(words: list[Word]) -> bool:
    """Whether the transcript marks sentence ends, which the cut-point checks rely on."""
    ends = sum(1 for w in words if SENTENCE_END.search(w.word))
    return bool(words) and ends >= PUNCTUATED_MIN * len(words)


# --- clips -----------------------------------------------------------------------


def _spoken_in(w: Word, start_ms: int, end_ms: int) -> bool:
    """Whether a word is spoken inside [start, end): it overlaps the span, or takes no time and falls
    in it. A word that only touches an edge belongs to the neighbouring span: caption words are
    contiguous (each ends where the next starts) and Highlightr's clips start and end on row edges."""
    return w.start_ms < end_ms and (w.end_ms > start_ms or w.start_ms >= start_ms)


def word_bounds(words: list[Word], start_ms: int, end_ms: int) -> tuple[int, int] | None:
    """The inclusive word range a time span covers: from the first to the last word spoken
    inside it, partly covered words included. None if no word falls inside it."""
    first = next((w.seq for w in words if _spoken_in(w, start_ms, end_ms)), None)
    if first is None:
        return None
    return first, next(w.seq for w in reversed(words) if _spoken_in(w, start_ms, end_ms))


def _normalized(token: str) -> str:
    return re.sub(r"[\W_]", "", token.casefold())


def quote_range(quote: str, words: list[Word], first: int, last: int) -> tuple[int, int] | None:
    """Where a quote occurs verbatim (ignoring case and punctuation) inside a word range."""
    wanted = [t for t in (_normalized(part) for part in quote.split()) if t]
    span = [(w.seq, t) for w in words[first - 1:last] if (t := _normalized(w.word))]
    tokens = [t for _, t in span]
    for i in range(len(span) - len(wanted) + 1 if wanted else 0):
        if tokens[i:i + len(wanted)] == wanted:
            return span[i][0], span[i + len(wanted) - 1][0]
    return None


def passages(words: list[Word], first: int, last: int) -> list[Passage]:
    """Split a word range into consecutive chunks of at most ~20 seconds."""
    span = words[first - 1:last]
    out, start = [], 0
    while start < len(span):
        end = start
        while end + 1 < len(span) and span[end + 1].end_ms - span[start].start_ms <= PASSAGE_MS:
            end += 1
        chunk = span[start:end + 1]
        text = full_text(chunk)
        out.append(Passage(chunk[0].seq, chunk[-1].seq, text, sha256(text)))
        start = end + 1
    return out


def build_clips(result: dict, words: list[Word]) -> list[NewClip]:
    """Highlightr's ranked clips, anchored to the transcript's words.

    `title` and `summary` are Highlightr's polished headline and description, falling
    back to its plain title and summary; the raw clip stays in `details`. Clips that
    cover no words are dropped.
    """
    clips: list[NewClip] = []
    for raw in sorted(result.get("clips") or [], key=lambda c: c.get("rank") or 0):
        start, end = ms(raw["start_seconds"]), ms(raw["end_seconds"])
        bounds = word_bounds(words, start, end) if 0 <= start < end else None
        if bounds is None:
            log.warning("dropping clip %r: no transcript words in %s-%s ms", raw.get("clip_id"), start, end)
            continue
        first, last = bounds
        quotes = []
        for text in raw.get("best_quotes") or []:
            if isinstance(text, str) and text.strip():
                found = quote_range(text, words, first, last)
                quotes.append(Quote(len(quotes) + 1, text.strip(), *(found or (None, None))))
        title = str(raw.get("best_headline") or raw.get("title") or "").strip()
        summary = raw.get("best_description") or raw.get("summary")
        clips.append(NewClip(
            slug=new_slug(),
            local_clip_id=raw.get("clip_id"),
            rank=int(raw.get("rank") or len(clips) + 1),
            title=title or f"Clip {len(clips) + 1}",
            summary=summary.strip() if isinstance(summary, str) and summary.strip() else None,
            start_ms=start,
            end_ms=end,
            first_word_seq=first,
            last_word_seq=last,
            overall_score=(raw.get("scores") or {}).get("overall"),
            details=raw,
            quotes=quotes,
            passages=passages(words, first, last),
        ))
    return clips
