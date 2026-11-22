"""Turn Parakeet ``verbose_json`` into word timings, caption rows, and an SRT.

The alignment and cue-grouping logic is ported from RipeClips'
``youtube_to_highlights.py`` so both apps feed Highlightr identical input.
"""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[\w]+(?:['’][\w]+)*", re.UNICODE)
SENTENCE_BREAK_RE = re.compile(r"[.!?][\"')\]]*$")

# Same row shape RipeClips sends to Highlightr: compact rows, exact word timing.
CUE_SETTINGS = {
    "max_duration": 8.0,
    "max_words_per_line": 6,
    "rows": 4,
    "pause": 0.6,
}


class TranscriptError(RuntimeError):
    pass


def _is_timed_character(character: str) -> bool:
    return character.isalnum() or character == "_"


def _fragments(data: dict) -> list[dict]:
    fragments = data.get("words")
    if not isinstance(fragments, list):
        raise TranscriptError("Parakeet response did not contain word timestamps")
    out = []
    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue
        try:
            start = float(fragment["start"])
            end = float(fragment["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise TranscriptError("Parakeet returned an invalid word timestamp") from error
        if not end >= start >= 0:
            raise TranscriptError("Parakeet returned an invalid word timestamp range")
        out.append({"word": str(fragment.get("word") or ""), "start": start, "end": end})
    return out


def _aligned_words(transcript: str, fragments: list[dict]) -> list[dict]:
    """Map sub-word timing fragments onto the punctuated transcript's words."""
    char_times: list[tuple[str, float, float]] = []
    for fragment in fragments:
        characters = [c.lower() for c in fragment["word"] if _is_timed_character(c)]
        if not characters:
            continue
        step = (fragment["end"] - fragment["start"]) / len(characters)
        char_times.extend(
            (c, fragment["start"] + i * step, fragment["start"] + (i + 1) * step)
            for i, c in enumerate(characters)
        )

    words: list[dict] = []
    cursor = 0
    matches = list(WORD_RE.finditer(transcript))
    for index, match in enumerate(matches):
        text = match.group(0)
        characters = [c.lower() for c in text if _is_timed_character(c)]
        available = char_times[cursor : cursor + len(characters)]
        if len(available) != len(characters) or any(
            timed[0] != expected for timed, expected in zip(available, characters)
        ):
            raise TranscriptError(f'Could not align timestamps to word "{text}"')
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(transcript)
        suffix = transcript[match.end() : next_start].strip()
        words.append(
            {
                "text": text + suffix,
                "start_seconds": round(available[0][1], 3),
                "end_seconds": round(available[-1][2], 3),
            }
        )
        cursor += len(characters)
    if cursor != len(char_times):
        raise TranscriptError("Timestamps contain text absent from the transcript")
    return words


def timed_words(data: dict) -> list[dict]:
    """Return ``[{text, start_seconds, end_seconds}]`` for every spoken word."""
    fragments = _fragments(data)
    try:
        words = _aligned_words(str(data.get("text") or ""), fragments)
    except TranscriptError:
        # Fall back to the raw fragments rather than failing the whole episode.
        words = [
            {
                "text": f["word"].strip(),
                "start_seconds": round(f["start"], 3),
                "end_seconds": round(f["end"], 3),
            }
            for f in fragments
            if f["word"].strip()
        ]
    if not words:
        raise TranscriptError("Parakeet returned no timestamped words")
    return words


def make_rows(
    words: list[dict],
    *,
    max_duration: float = CUE_SETTINGS["max_duration"],
    max_words_per_line: int = CUE_SETTINGS["max_words_per_line"],
    rows: int = CUE_SETTINGS["rows"],
    pause: float = CUE_SETTINGS["pause"],
) -> list[dict]:
    """Group words into caption rows that break on pauses and sentence ends."""
    grouped: list[list[dict]] = []
    current: list[dict] = []
    max_words = max_words_per_line * rows
    for word in words:
        candidate = [*current, word]
        duration = word["end_seconds"] - candidate[0]["start_seconds"]
        gap = word["start_seconds"] - current[-1]["end_seconds"] if current else 0.0
        if current and (duration > max_duration or len(candidate) > max_words or gap >= pause):
            grouped.append(current)
            current = [word]
        else:
            current = candidate
        if SENTENCE_BREAK_RE.search(word["text"]):
            grouped.append(current)
            current = []
    if current:
        grouped.append(current)

    out = []
    for group in grouped:
        start = group[0]["start_seconds"]
        out.append(
            {
                "id": len(out),
                "start_seconds": start,
                "end_seconds": max(group[-1]["end_seconds"], round(start + 0.2, 3)),
                "text": " ".join(w["text"] for w in group),
            }
        )
    return out


def srt_timestamp(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def rows_to_srt(rows: list[dict]) -> str:
    blocks = [
        f"{i}\n{srt_timestamp(r['start_seconds'])} --> {srt_timestamp(r['end_seconds'])}\n{r['text']}"
        for i, r in enumerate(rows, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


# --- YouTube captions -------------------------------------------------------

CAPTION_NOISE_RE = re.compile(r"\[[^\]]*\]|<[^>]+>|[♪♫]|^\s*>>|(?<=\s)>>")
MAX_WORD_SECONDS = 1.0


def _clean_caption(text: str) -> str:
    return " ".join(CAPTION_NOISE_RE.sub(" ", text.replace("\n", " ")).split())


def caption_words(data: dict) -> list[dict]:
    """Convert a YouTube json3 caption track into the same word list Parakeet yields.

    Auto-generated tracks time every word; creator-uploaded tracks time whole
    cues, so their words are spread across the cue by length.
    """
    events = [e for e in data.get("events") or [] if e.get("segs") and "tStartMs" in e]
    word_timed = any("tOffsetMs" in seg for e in events for seg in e["segs"])
    words: list[dict] = []
    for event in events:
        segs = event["segs"]
        start = event["tStartMs"] / 1000
        end = start + (event.get("dDurationMs") or 0) / 1000
        if word_timed:
            for seg in segs:
                at = start + (seg.get("tOffsetMs") or 0) / 1000
                for token in _clean_caption(seg.get("utf8") or "").split():
                    words.append({"text": token, "start_seconds": at, "end_seconds": None, "cap": end})
        else:
            tokens = _clean_caption("".join(seg.get("utf8") or "" for seg in segs)).split()
            weight = sum(len(t) + 1 for t in tokens)
            at = start
            for token in tokens:
                step = (end - start) * (len(token) + 1) / weight
                words.append({"text": token, "start_seconds": at, "end_seconds": at + step, "cap": end})
                at += step

    words.sort(key=lambda w: w["start_seconds"])
    out = []
    for index, word in enumerate(words):
        end = word["end_seconds"]
        if end is None:
            following = words[index + 1]["start_seconds"] if index + 1 < len(words) else word["cap"]
            end = min(following, word["cap"], word["start_seconds"] + MAX_WORD_SECONDS)
        start = round(word["start_seconds"], 3)
        out.append({"text": word["text"], "start_seconds": start,
                    "end_seconds": max(round(end, 3), round(start + 0.05, 3))})
    if not out:
        raise TranscriptError("YouTube captions contained no words")
    return out
