"""Heuristics about the words around a clip.

Yolt keeps every word of a transcript, so a clip's surroundings are read
straight from the word table; nothing is aligned or approximated.
"""

from __future__ import annotations

_CLOSERS = "\"'”’)]"


def starts_mid_sentence(before: str) -> bool:
    """The text before the clip doesn't end a sentence."""
    text = before.rstrip().rstrip(_CLOSERS)
    return bool(text) and text[-1] not in ".?!"


def follows_question(before: str) -> bool:
    """The text before the clip ends with a question the clip may depend on."""
    return before.rstrip().rstrip(_CLOSERS).endswith("?")


def ends_mid_sentence(text: str) -> bool:
    text = text.rstrip().rstrip(_CLOSERS)
    return bool(text) and text[-1] not in ".?!"
