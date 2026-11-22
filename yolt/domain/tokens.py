"""Word normalization shared by text anchors and full-text alignment.

A transcript word can carry punctuation ("like,"), apostrophes ("that's"),
ASR artifacts ("our$") or hyphen fragments ("winner-", "well-known"). Each word
is split into normalized subtokens so that phrases match regardless of how the
ASR happened to split or punctuate them.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_SPLIT = re.compile(r"(?:[^\w']|_)+")  # anything but letters, digits (any script) and apostrophes


def normalize(text: str) -> list[str]:
    """Lowercased alphanumeric subtokens; apostrophes are dropped, other punctuation splits."""
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'").replace("‘", "'")
    return [part.replace("'", "") for part in _SPLIT.split(text) if part.replace("'", "")]


@dataclass(frozen=True)
class Token:
    norm: str
    seq: int  # transcript word sequence number the subtoken came from


def tokenize_words(words: list[tuple[int, str]]) -> list[Token]:
    """[(seq, word)] in order -> subtokens, each remembering its source word."""
    return [Token(norm, seq) for seq, word in words for norm in normalize(word)]

