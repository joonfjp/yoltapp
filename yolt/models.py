"""Request bodies for compilations, reviews and renders (ported from clipit_api).

All closed (extra="forbid"): unknown fields are 422 `unknown_field`, and an optional
field is omitted, never sent as null."""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="before")
    @classmethod
    def not_null(cls, value):
        if value is None:
            raise PydanticCustomError("invalid_value", "Omit the field instead of sending null.")
        return value


class WordRange(Closed):
    first: Annotated[int, Field(ge=1)]
    last: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def ordered(self):
        if self.last < self.first:
            raise PydanticCustomError("words_reversed", "`last` ({last}) is before `first` ({first}).",
                                      {"first": self.first, "last": self.last})
        return self


class TextAnchors(Closed):
    from_: Annotated[str | None, Field(alias="from", min_length=1, max_length=300)] = None
    to: Annotated[str | None, Field(min_length=1, max_length=300)] = None
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)

    @model_validator(mode="after")
    def not_empty(self):
        if self.from_ is None and self.to is None:
            raise PydanticCustomError("invalid_value", "Give `from`, `to`, or both.")
        return self


class SegmentSelection(Closed):
    clip_id: int
    words: WordRange | None = None
    text: TextAnchors | None = None

    @model_validator(mode="after")
    def one_trim(self):
        if self.words is not None and self.text is not None:
            raise PydanticCustomError("invalid_value", "Use either `words` or `text`, not both.")
        return self

    def as_dict(self) -> dict:
        out: dict = {"clip_id": self.clip_id}
        if self.words is not None:
            out["words"] = {"first": self.words.first, "last": self.words.last}
        if self.text is not None:
            out["text"] = {k: v for k, v in (("from", self.text.from_), ("to", self.text.to)) if v is not None}
        return out


Items = Annotated[list[SegmentSelection], Field(min_length=1, max_length=200)]
Title = Annotated[str, Field(min_length=1, max_length=200)]
METADATA_MAX_BYTES = 16384


def metadata_size(value: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(value, separators=(",", ":")).encode()) > METADATA_MAX_BYTES:
        raise PydanticCustomError("invalid_value", "`metadata` must be at most {limit} bytes of JSON.",
                                  {"limit": METADATA_MAX_BYTES})
    return value


# Free-form context for a revision (theme, query, notes on each item); stored as given.
Metadata = Annotated[dict[str, Any], AfterValidator(metadata_size)]


class ReviewRequest(Closed):
    segments: Items
    target_duration_ms: Annotated[int, Field(ge=1)] | None = None


class NewCompilation(Closed):
    title: Title
    items: Items
    metadata: Metadata | None = None


class NewRevision(Closed):
    based_on: Annotated[int, Field(ge=1)]
    items: Items
    metadata: Metadata | None = None


MAX_RENDER_MS = 86_400_000  # a day: far past any real render, well inside bigint


class NewRender(Closed):
    format: Literal["long", "short"]
    output_path: Annotated[str, Field(min_length=1)]
    duration_ms: Annotated[int, Field(ge=1, le=MAX_RENDER_MS)]
    spec_path: str | None = None
    positions: Annotated[list[Annotated[int, Field(ge=1)]], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def unique_positions(self):
        if self.positions is not None and len(set(self.positions)) != len(self.positions):
            raise PydanticCustomError("invalid_value", "`positions` must not repeat.")
        return self

