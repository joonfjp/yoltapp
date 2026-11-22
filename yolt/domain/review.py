"""Editorial checks on a compilation's items: repeats, drift, cut points, balance and length.

Thresholds were calibrated on the dev catalogue's 747 compilation items (Qwen3-Embedding-8B@1024,
2026-10-24): 0.75 is the top 0.1% of item pairs within a compilation, where the pairs are mostly
genuine repeats. Similarity to the rest of a compilation is a weak signal (on-theme items can
score low), so drift is only `info`, and only when an item is well below the compilation's own
median.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from statistics import median

from .context import ends_mid_sentence, follows_question, starts_mid_sentence

NEAR_DUPLICATE = 0.75
OFF_THEME_BELOW = 0.45
OFF_THEME_GAP = 0.15
SHARE = 0.4  # more than this share of the items from one episode, or with one featured speaker
MIN_SHARED = 3
TARGET_TOLERANCE = 0.10
SHORT_MS = 3_000
LONG_MS = 90_000  # without a target; with one, an item longer than half of it is long

SEMANTIC_CHECKS = ("near_duplicate", "off_theme")


@dataclass
class Item:
    position: int
    clip_id: int
    episode_id: int
    duration_ms: int
    text: str  # the item as it reads in the full transcript
    before: str  # the transcript just before it
    alignment: str
    speakers: list[str] = field(default_factory=list)
    vector: list[float] | None = None
    transcript_id: int = 0
    words: tuple[int, int] = (0, 0)  # inclusive seq range
    time: tuple[int, int] = (0, 0)  # start_ms, end_ms
    spoken: str = ""  # the item's own timed words
    punctuated: bool = True  # whether its transcript marks sentence ends (YouTube auto-captions don't)


@dataclass
class Finding:
    code: str
    severity: str  # "warning" | "info"
    positions: list[int]
    message: str
    similarity: float | None = None

    def to_json(self) -> dict:
        out = {"code": self.code, "severity": self.severity, "positions": self.positions, "message": self.message}
        if self.similarity is not None:
            out["similarity"] = round(self.similarity, 4)
        return out


def _unit(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _mean(vectors: list[list[float]]) -> list[float]:
    return [sum(col) / len(vectors) for col in zip(*vectors, strict=True)]


def _from_context(item: Item) -> str:
    return " (approximate alignment)" if item.alignment != "exact" else ""


def review(items: list[Item], *, target_duration_ms: int | None, vectors: bool) -> dict:
    """The review of these items, in order. `vectors` is whether the catalogue has vectors at all."""
    findings: list[Finding] = []
    theme: dict[int, float | None] = {i.position: None for i in items}
    long_ms = target_duration_ms // 2 if target_duration_ms else LONG_MS

    # Cut points. They read sentence punctuation, so an unpunctuated transcript can't be checked: say so
    # once instead of warning on every item (or never noticing a question).
    unchecked = [i.position for i in items if not i.punctuated]
    if unchecked:
        which = f"Item {unchecked[0]} comes" if len(unchecked) == 1 else f"Items {', '.join(map(str, unchecked))} come"
        findings.append(Finding("cut_points_unchecked", "info", unchecked,
                                f"{which} from a transcript without punctuation (YouTube auto-captions), "
                                "so where they start and end wasn't checked."))
    for i in items:
        if i.punctuated and i.before and _starts_mid(i):
            findings.append(Finding("starts_mid_sentence", "warning", [i.position],
                                    f"Item {i.position} starts mid-sentence{_from_context(i)}."))
        if i.punctuated and _follows_question(i):
            findings.append(Finding("follows_question", "warning", [i.position],
                                    f"Item {i.position} answers a question it doesn't include{_from_context(i)}."))
        if i.punctuated and _ends_mid(i):
            findings.append(Finding("ends_mid_sentence", "warning", [i.position],
                                    f"Item {i.position} ends mid-sentence{_from_context(i)}."))
        if i.duration_ms < SHORT_MS:
            findings.append(Finding("item_too_short", "info", [i.position],
                                    f"Item {i.position} is {i.duration_ms / 1000:.1f} s long."))
        elif i.duration_ms > long_ms:
            findings.append(Finding("item_too_long", "info", [i.position],
                                    f"Item {i.position} is {i.duration_ms / 1000:.0f} s long."))

    # Repeats and drift.
    embedded = [i for i in items if i.vector is not None]
    unit = {i.position: _unit(i.vector) for i in embedded}
    for n, a in enumerate(embedded):
        for b in embedded[n + 1:]:
            if a.clip_id == b.clip_id:
                continue  # two cuts from one clip are deliberate
            sim = _dot(unit[a.position], unit[b.position])
            if sim >= NEAR_DUPLICATE:
                findings.append(Finding("near_duplicate", "warning", [a.position, b.position],
                                        f"Items {a.position} and {b.position} may make the same point.", sim))
    if len(embedded) >= 3:
        for i in embedded:
            others = _unit(_mean([unit[o.position] for o in embedded if o is not i]))
            theme[i.position] = _dot(unit[i.position], others)
        typical = median(t for t in theme.values() if t is not None)
        for i in embedded:
            t = theme[i.position]
            if t < OFF_THEME_BELOW and t < typical - OFF_THEME_GAP:
                findings.append(Finding("off_theme", "info", [i.position],
                                        f"Item {i.position} is the least connected to the rest; check it belongs.", t))

    # Balance.
    for code, groups, what in (("episode_heavy", {i.position: [i.episode_id] for i in items}, "episode"),
                               ("speaker_heavy", {i.position: i.speakers for i in items}, "featured speaker")):
        counts = Counter(g for gs in groups.values() for g in set(gs))
        for group, count in counts.items():
            if count >= MIN_SHARED and count > SHARE * len(items):
                positions = [p for p, gs in groups.items() if group in gs]
                episodes = {i.episode_id for i in items if i.position in positions}
                if code == "speaker_heavy" and len(episodes) == 1:
                    continue  # already reported as episode_heavy
                label = f" ({group})" if code == "speaker_heavy" else ""
                note = " Speakers are attributed per episode, not per clip." if code == "speaker_heavy" else ""
                findings.append(Finding(code, "info", positions,
                                        f"{count} of {len(items)} items share one {what}{label}.{note}"))

    # Length.
    total = sum(i.duration_ms for i in items)
    if target_duration_ms and abs(total - target_duration_ms) > TARGET_TOLERANCE * target_duration_ms:
        findings.append(Finding("duration_off_target", "warning", [],
                                f"The items run {total / 1000:.0f} s against a target of {target_duration_ms / 1000:.0f} s."))

    findings.sort(key=lambda f: (f.severity != "warning", f.positions[:1] or [0], f.code))
    return {
        "item_count": len(items), "duration_ms": total, "target_duration_ms": target_duration_ms,
        "findings": [f.to_json() for f in findings],
        "skipped": [] if vectors else list(SEMANTIC_CHECKS),
        "items": [{"position": i.position, "clip_id": i.clip_id, "transcript_id": i.transcript_id,
                   "words": {"first": i.words[0], "last": i.words[1]},
                   "time": {"start_ms": i.time[0], "end_ms": i.time[1]}, "duration_ms": i.duration_ms,
                   "text": i.spoken, "before": i.before, "alignment": i.alignment,
                   "theme_similarity": None if theme[i.position] is None else round(theme[i.position], 4),
                   "starts_mid_sentence": (bool(i.before) and _starts_mid(i)) if i.punctuated else None,
                   "follows_question": _follows_question(i) if i.punctuated else None,
                   "ends_mid_sentence": _ends_mid(i) if i.punctuated else None} for i in items],
    }


def _starts_mid(i: Item) -> bool:
    return starts_mid_sentence(i.before)


def _follows_question(i: Item) -> bool:
    return follows_question(i.before)


def _ends_mid(i: Item) -> bool:
    return ends_mid_sentence(i.text)
