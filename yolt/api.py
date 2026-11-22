"""The HTTP API under /api/v1, implementing spec/yolt.openapi.json.

Conventions (ported from clipit_api): errors are RFC 9457 problem+json listing every
problem at once; a query parameter an operation doesn't define is a 400, never
silently ignored; lists page with opaque cursors; times are integer milliseconds
and word ranges are inclusive.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal, get_origin

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.routing import Match

from . import labs, services, subscriptions, youtube
from .db import PLATFORM, SETTINGS, Database
from .domain import feed as feed_picker
from .domain import ranges
from .embeddings import EmbeddingService, EmbeddingUnavailable
from .models import Closed, NewCompilation, NewRender, NewRevision, ReviewRequest
from .problems import ApiProblem, ErrorList, FieldError, bad_request, invalid, pointer, unstorable
from .repo import clips as clips_repo
from .repo import compilations as comp_repo
from .repo import episodes as ep_repo
from .repo import feed as feed_repo
from .repo import renders as render_repo
from .repo import review as review_repo
from .repo import runs as runs_repo
from .repo import speakers as speakers_repo
from .repo import stats as stats_repo
from .repo import subscriptions as subs_repo
from .repo import themes as theme_repo
from .repo import vectors as vector_repo

API_VERSION = "0.9.0"
PREFIX = "/api/v1"
PROBLEM_JSON = "application/problem+json"
MAX_WORD_SPAN = 5000
MAX_INSTRUCTIONS = 1000
COMPACT_LIMIT = 1000
MAX_CANDIDATES = 10  # per discovery window; more mostly lengthens prompts and the ranking pass
MAX_CHECK_HOURS = 168  # subscriptions are checked at least weekly unless checks are off (0)
MAX_MIN_MINUTES = 600
# On every response (server.py adds them; the crash handler runs outside middleware, so it adds its own):
# no other site may frame Yolt's pages to clickjack their buttons, and nothing is content-sniffed.
SECURITY_HEADERS = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY",
                    "X-Content-Type-Options": "nosniff"}


# --- plumbing -----------------------------------------------------------------

def respond(status: int, payload: Any, headers: dict[str, str] | None = None) -> Response:
    media = PROBLEM_JSON if status >= 400 else "application/json"
    return JSONResponse(payload, status_code=status, headers=headers or {}, media_type=media)


def get_db(request: Request) -> Database:
    return request.app.state.db


def request_settings(request: Request) -> dict[str, str]:
    """Settings as the request starts: read once, before its connection is taken, so nothing later in the
    request takes a second pooled connection to read them."""
    return request.app.state.db.settings()


Settings = Annotated[dict[str, str], Depends(request_settings)]


def connection(request: Request, _: Settings) -> Iterator[psycopg.Connection]:
    with request.app.state.db.connect() as conn:
        yield conn


def embeddings(request: Request, settings: Settings) -> EmbeddingService:
    return request.app.state.embeddings.pinned(settings)


def base_url(request: Request) -> str:
    """Where the request came in, for the absolute links clips carry."""
    return str(request.base_url)


Conn = Annotated[psycopg.Connection, Depends(connection)]
Db = Annotated[Database, Depends(get_db)]
Base = Annotated[str, Depends(base_url)]
Embed = Annotated[EmbeddingService, Depends(embeddings)]
Limit = Annotated[int, Query(ge=1, le=200)]


def parse_include(raw: list[str], allowed: set[str]) -> set[str]:
    """`include` is a comma list, and may also be repeated."""
    values = {v.strip() for part in raw for v in part.split(",") if v.strip()}
    unknown = values - allowed
    if unknown:
        raise bad_request([FieldError("invalid_value", f"Unknown include value(s): {', '.join(sorted(unknown))}. "
                                      f"Allowed: {', '.join(sorted(allowed))}.", parameter="include")])
    return values


def page(data: list[dict], next_cursor: str | None) -> dict:
    return {"data": data, "next_cursor": next_cursor}


KNOWN_CODES = {"invalid_value", "words_reversed"}
TIMES_DERIVED = ("start_ms", "end_ms", "transcript_id", "transcript_version_id")


def validation_problem(exc: RequestValidationError) -> ApiProblem:
    errors: list[FieldError] = []
    body_errors = malformed = False
    for err in exc.errors():
        loc = list(err.get("loc", ()))
        source = loc[0] if loc else "body"
        kind = err.get("type", "invalid_value")
        malformed |= kind == "json_invalid"
        code = {"extra_forbidden": "unknown_field", "missing": "missing_field"}.get(
            kind, kind if kind in KNOWN_CODES else "invalid_value")
        message = err.get("msg", "Invalid value.")
        if kind == "extra_forbidden" and loc and loc[-1] in TIMES_DERIVED:
            message = "Times and transcript are derived from words; send `words` instead."
        if source == "body":
            body_errors = True
            errors.append(FieldError(code, message, pointer=pointer(*loc[1:])))
        else:
            errors.append(FieldError(code, message, parameter=str(loc[1]) if len(loc) > 1 else str(source)))
    if malformed:
        return bad_request(errors, "The body is not valid JSON")
    return invalid(errors) if body_errors else bad_request(errors)


def json_only(request: Request) -> None:
    """Bodies are JSON. A form or text body (what a cross-site HTML form can send) is refused up front."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return
    if request.headers.get("content-length", "0") == "0" and "transfer-encoding" not in request.headers:
        return
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json" and not media.endswith("+json"):
        raise ApiProblem(415, "unsupported-media-type", "Unsupported media type",
                         detail="Send the body as JSON with `Content-Type: application/json`.",
                         headers={"Accept-Post": "application/json"})


def _query_params(dependant) -> dict[str, bool]:
    """Each query parameter the operation (and its dependencies) defines, and whether it may repeat."""
    params = {p.alias: get_origin(p.field_info.annotation) is list for p in dependant.query_params}
    for sub in dependant.dependencies:
        params |= _query_params(sub)
    return params


_QUERY_PARAMS: dict[str, dict[str, bool]] = {}


def known_parameters(request: Request) -> None:
    """Reject query parameters the operation doesn't define, and single-value ones sent twice."""
    route = request.scope.get("route")
    if route is None:
        return
    params = _QUERY_PARAMS.get(route.unique_id)
    if params is None:
        params = _QUERY_PARAMS[route.unique_id] = _query_params(route.dependant)
    errors = [FieldError("unknown_parameter", f"`{name}` isn't a parameter of this operation."
                         + (f" Allowed: {', '.join(sorted(params))}." if params else ""), parameter=name)
              for name in sorted(set(request.query_params) - set(params))]
    errors += [FieldError("invalid_value", f"`{name}` takes one value; it was sent {count} times.", parameter=name)
               for name, count in sorted(Counter(k for k, _ in request.query_params.multi_items()).items())
               if count > 1 and params.get(name) is False]
    if errors:
        raise bad_request(errors)


BAD_TEXT = "Text can't contain NUL (U+0000) or an unpaired surrogate."
BAD_NAME = "A member name here contains NUL (U+0000) or an unpaired surrogate."


def _unstorable_in(value: Any) -> Iterator[tuple[tuple, str]]:
    """Where in a JSON value a string can't be stored, in document order. A bad member name is reported at
    its object, since a pointer to it couldn't be sent back either."""
    stack: list[tuple[Any, tuple]] = [(value, ())]
    while stack:  # no recursion: the body may nest as deep as the JSON parser allows
        node, at = stack.pop()
        if isinstance(node, str):
            if unstorable(node):
                yield at, BAD_TEXT
        elif isinstance(node, dict):
            if any(unstorable(k) for k in node):
                yield at, BAD_NAME
            stack.extend(reversed([(v, (*at, k)) for k, v in node.items() if not unstorable(k)]))
        elif isinstance(node, list):
            stack.extend(reversed([(v, (*at, i)) for i, v in enumerate(node)]))


async def storable_text(request: Request) -> None:
    """Refuse NUL and unpaired surrogates in any parameter (400) or body string (422) up front: PostgreSQL
    can't store NUL and UTF-8 can't encode a lone surrogate, so either would otherwise end in a 500."""
    errors = [FieldError("invalid_value", BAD_TEXT, parameter=name)
              for name, value in request.path_params.items() if isinstance(value, str) and unstorable(value)]
    errors += [FieldError("invalid_value", BAD_TEXT, parameter=name)
               for name in sorted({name for name, value in request.query_params.multi_items() if unstorable(value)})]
    body_errors = []
    route = request.scope.get("route")
    if getattr(route, "body_field", None) is not None and await request.body():
        body = await request.json()  # already parsed (and cached) by FastAPI, which refuses one that isn't JSON
        body_errors = [FieldError("invalid_value", message, pointer=pointer(*at))
                       for at, message in _unstorable_in(body)]
    if body_errors:
        raise invalid(errors + body_errors)
    if errors:
        raise bad_request(errors)


router = APIRouter(prefix=PREFIX,
                   dependencies=[Depends(json_only), Depends(known_parameters), Depends(storable_text)])


# --- service ------------------------------------------------------------------

@router.get("/")
def get_service(conn: Conn, embed: Embed):
    version = conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"]
    return {"api_version": API_VERSION, "schema_version": version, "queue": runs_repo.queue_counts(conn),
            "embeddings": vector_repo.coverage(conn, embed)}


# --- runs ---------------------------------------------------------------------

class RunOptions(BaseModel):
    """A run's own clip options, each overriding its setting (the same names) for that run; the same rules."""
    model_config = ConfigDict(extra="forbid")
    min_clip_seconds: StrictInt | None = Field(default=None, ge=1)
    max_clip_seconds: StrictInt | None = Field(default=None, ge=1)
    max_clips: StrictInt | None = Field(default=None, ge=1)
    candidates_per_window: StrictInt | None = Field(default=None, ge=1, le=MAX_CANDIDATES)


class NewRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1)
    instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS)
    options: RunOptions | None = None


def _run_options(body: NewRun, settings: dict[str, str]) -> dict[str, int]:
    """The options given (none: {}, the run follows Settings), checked against the settings they leave as they are:
    the shortest clip can't be longer than the longest."""
    options = body.options.model_dump(exclude_none=True) if body.options else {}
    shortest = options.get("min_clip_seconds", _whole(settings["min_clip_seconds"]))
    longest = options.get("max_clip_seconds", _whole(settings["max_clip_seconds"]))
    if shortest is not None and longest is not None and shortest > longest:
        key = "min_clip_seconds" if "min_clip_seconds" in options else "max_clip_seconds"
        where = "" if {"min_clip_seconds", "max_clip_seconds"} <= options.keys() else " (the other is from Settings)"
        raise invalid([FieldError("invalid_value", f"Minimum clip length ({shortest}s) can't exceed the maximum "
                                  f"({longest}s){where}.", pointer=f"/options/{key}")])
    return options


def _video_id(url: str, *, at: str | None = None, parameter: str | None = None) -> str:
    try:
        return youtube.parse_video_id(url)
    except youtube.YouTubeError as error:
        raise invalid([FieldError("not_youtube", str(error), pointer=at, parameter=parameter)]) from error


def _video_info(request: Request, video_id: str, *, at: str | None = None, parameter: str | None = None) -> dict:
    try:
        return request.app.state.worker.info_cache.get(video_id)
    except youtube.YouTubeError as error:
        raise invalid([FieldError("video_unavailable", str(error), pointer=at, parameter=parameter)]) from error


@router.get("/lookup")
def lookup(request: Request, db: Db, embed: Embed, url: Annotated[str, Query(min_length=1)]):
    video_id = _video_id(url, parameter="url")
    info = _video_info(request, video_id, parameter="url")  # yt-dlp can take seconds: no connection held
    meta = youtube.metadata(info)
    creator = youtube.pick_caption_track(info, "manual")
    auto = youtube.pick_caption_track({**info, "subtitles": {}}, "any")
    chosen = youtube.pick_caption_track(info, db.settings()["youtube_captions"])
    channel_id = info.get("channel_id")  # what POST /subscriptions with this video's link would follow
    if not (isinstance(channel_id, str) and youtube.CHANNEL_ID_RE.match(channel_id)):
        channel_id = None
    with db.connect() as conn:
        subscription = subs_repo.for_channel(conn, channel_id)
    return {
        "video_id": video_id,
        "url": youtube.watch_url(video_id),
        "title": meta["title"],
        "channel": meta["channel"],
        "channel_url": meta["channel_url"],
        "channel_id": channel_id,
        "subscription": subscription,
        "published_at": meta["published_at"],
        "duration_ms": round(meta["duration_seconds"] * 1000) if meta["duration_seconds"] else None,
        "thumbnail_url": youtube.small_thumbnail_url(video_id),
        "captions": {"creator": creator and creator.language, "auto": auto and auto.language},
        "transcript_source": chosen.source if chosen else "parakeet",
        "runs": _runs_for_video(db, video_id, embed.model_name),
    }


def _runs_for_video(db: Database, video_id: str, model: str) -> list[dict]:
    with db.connect() as conn:
        return runs_repo.runs_for_video(conn, PLATFORM, video_id, model)


@router.post("/runs")
def create_run(request: Request, db: Db, embed: Embed, body: NewRun):
    worker = request.app.state.worker
    video_id = _video_id(body.url, at="/url")
    settings = db.settings()
    instructions = (body.instructions or "").strip() or settings["clip_instructions"].strip()
    options = _run_options(body, settings)
    info = _video_info(request, video_id, at="/url")
    run_id = db.submit(video_id, instructions, youtube.metadata(info), options)  # a new run every time
    worker.wake()
    with db.connect() as conn:
        run = runs_repo.get_run(conn, run_id, set(), base_url(request), embed.model_name)
    return respond(202, run, {"Location": f"{PREFIX}/runs/{run_id}"})


@router.get("/runs")
def list_runs(conn: Conn, embed: Embed, episode_id: Annotated[list[int], Query()] = [],
              status: Annotated[list[Literal["queued", "processing", "ready", "failed", "cancelled"]], Query()] = [],
              limit: Limit = 25, cursor: str | None = None):
    return page(*runs_repo.list_runs(conn, episode_ids=episode_id, statuses=list(status), limit=limit,
                                     cursor=cursor, model=embed.model_name))


@router.get("/queue")
def get_queue(conn: Conn, embed: Embed):
    return {**runs_repo.queue(conn, embed.model_name), "totals": runs_repo.totals(conn)}


@router.get("/stats")
def get_stats(conn: Conn):
    return stats_repo.home(conn)


@router.get("/runs/{run_id}")
def get_run(conn: Conn, embed: Embed, base: Base, run_id: int, include: Annotated[list[str], Query()] = []):
    return runs_repo.get_run(conn, run_id, parse_include(include, {"clips"}), base, embed.model_name)


@router.post("/runs/{run_id}/retry")
def retry_run(request: Request, db: Db, embed: Embed, run_id: int):
    run = db.run(run_id)
    if run is None:
        raise ApiProblem(404, "not-found", "Run not found")
    if run["status"] not in ("failed", "cancelled"):
        raise ApiProblem(409, "not-failed", "Only failed or cancelled runs can be retried",
                         detail=f"Run {run_id} is {run['status']}.")
    request.app.state.worker.enqueue(run_id)
    with db.connect() as conn:
        return respond(202, runs_repo.get_run(conn, run_id, set(), base_url(request), embed.model_name))


@router.post("/runs/{run_id}/cancel")
def cancel_run(request: Request, db: Db, embed: Embed, run_id: int):
    if not db.cancel_run(run_id):
        run = db.run(run_id)
        if run is None:
            raise ApiProblem(404, "not-found", "Run not found")
        if run["status"] != "cancelled":  # cancelling twice is fine
            raise ApiProblem(409, "not-active", "Only queued or processing runs can be cancelled",
                             detail=f"Run {run_id} is {run['status']}.")
    with db.connect() as conn:
        return respond(200, runs_repo.get_run(conn, run_id, set(), base_url(request), embed.model_name))


# --- episodes and transcripts ---------------------------------------------------

@router.get("/episodes")
def list_episodes(conn: Conn, embed: Embed, q: Annotated[str | None, Query(min_length=1)] = None,
                  podcast_id: Annotated[list[int], Query()] = [], speaker_id: Annotated[list[int], Query()] = [],
                  published_after: date | None = None, limit: Limit = 25, cursor: str | None = None):
    return page(*ep_repo.list_episodes(conn, q=q, podcast_ids=podcast_id, speaker_ids=speaker_id,
                                       published_after=published_after and published_after.isoformat(),
                                       limit=limit, cursor=cursor, model=embed.model_name))


@router.get("/episodes/{episode_id}")
def get_episode(conn: Conn, embed: Embed, episode_id: int):
    return ep_repo.get_episode(conn, episode_id, embed.model_name)


class SpeakerName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    role: Literal["host", "guest"]


class SpeakersUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speakers: list[SpeakerName] = Field(max_length=50)


@router.put("/episodes/{episode_id}/speakers")
def set_episode_speakers(request: Request, db: Db, embed: Embed, episode_id: int, body: SpeakersUpdate):
    with db.connect() as conn:
        ep_repo.get_episode(conn, episode_id, embed.model_name)  # 404 if missing
    errors, seen, people = ErrorList(), set(), []
    for i, person in enumerate(body.speakers):
        name = " ".join(person.name.split())
        if not name:
            errors.add("invalid_value", "A name needs at least one letter.", at=pointer("speakers", i, "name"))
        elif name.casefold() in seen:
            errors.add("invalid_value", f"{name!r} is listed twice.", at=pointer("speakers", i, "name"))
        seen.add(name.casefold())
        people.append((name, person.role))
    errors.raise_if_any()
    db.set_speakers(episode_id, people)
    with db.connect() as conn:
        return ep_repo.get_episode(conn, episode_id, embed.model_name)


@router.get("/speakers")
def list_speakers(conn: Conn, q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
                  limit: Limit = 25, cursor: str | None = None):
    return page(*speakers_repo.list_speakers(conn, q=q, limit=limit, cursor=cursor))


@router.get("/transcripts/{transcript_id}")
def get_transcript(conn: Conn, transcript_id: int):
    return ep_repo.get_transcript(conn, transcript_id)


@router.get("/transcripts/{transcript_id}/text")
def get_transcript_text(conn: Conn, transcript_id: int,
                        range_header: Annotated[str | None, Header(alias="Range")] = None):
    data = ep_repo.transcript_text(conn, transcript_id).encode("utf-8")
    media = "text/plain; charset=utf-8"
    base = {"Accept-Ranges": "bytes"}
    if range_header:
        try:
            parsed = ranges.parse(range_header, len(data))
        except ranges.RangeError as exc:
            return respond(416, ApiProblem(416, "range-not-satisfiable", "Range not satisfiable",
                                           detail=str(exc)).body(),
                           {**base, "Content-Range": f"bytes */{len(data)}"})
        if parsed is not None:
            start, end = ranges.widen(data, *parsed)
            return Response(data[start:end + 1], status_code=206, media_type=media,
                            headers={**base, "Content-Range": f"bytes {start}-{end}/{len(data)}"})
    return Response(data, media_type=media, headers=base)


@router.get("/transcripts/{transcript_id}/srt")
def get_transcript_srt(conn: Conn, transcript_id: int):
    return Response(ep_repo.transcript_srt(conn, transcript_id), media_type="application/x-subrip; charset=utf-8")


@router.get("/transcripts/{transcript_id}/words")
def list_words(conn: Conn, transcript_id: int, first: Annotated[int, Query(ge=1)], last: Annotated[int, Query(ge=1)]):
    ep_repo.get_transcript(conn, transcript_id)
    errors = ErrorList()
    if last < first:
        errors.add("words_reversed", f"`last` ({last}) is before `first` ({first}).", parameter="last")
    elif last - first + 1 > MAX_WORD_SPAN:
        errors.add("word_span_too_large", f"A range can span at most {MAX_WORD_SPAN} words.", parameter="last")
    errors.raise_if_any()
    words = ep_repo.words(conn, transcript_id, first, last)
    if len(words) != last - first + 1:
        count = conn.execute("SELECT word_count FROM transcript_versions WHERE id = %s",
                             (transcript_id,)).fetchone()["word_count"]
        raise invalid([FieldError("words_missing", f"The transcript has {count} words; the range ends past it.",
                                  parameter="last")])
    return {"transcript_id": transcript_id, "words": words}


# --- clips --------------------------------------------------------------------

class ClipQuery:
    """The parameters that select clips, shared by GET /clips and GET /clips/facets."""

    def __init__(self, q: Annotated[str | None, Query(min_length=1)] = None,
                 search: Annotated[str | None, Query(pattern="^(keyword|semantic|hybrid)$")] = None,
                 like: Annotated[list[int], Query(max_length=vector_repo.MAX_LIKE)] = [],
                 min_similarity: Annotated[float | None, Query(ge=-1, le=1)] = None,
                 theme: str | None = None,
                 episode_id: Annotated[list[int], Query()] = [], transcript_id: Annotated[list[int], Query()] = [],
                 run_id: Annotated[list[int], Query()] = [], speaker_id: Annotated[list[int], Query()] = [],
                 min_score: float | None = None,
                 min_duration_ms: Annotated[int | None, Query(ge=0)] = None,
                 max_duration_ms: Annotated[int | None, Query(ge=0)] = None,
                 usage: Annotated[str | None, Query(pattern="^(never|picked|rendered)$")] = None,
                 exclude_id: Annotated[list[int], Query(max_length=200)] = [],
                 exclude_episode_id: Annotated[list[int], Query()] = []):
        self.usage = usage
        self.q, self.search, self.like, self.min_similarity, self.theme = (
            q, search, list(dict.fromkeys(like)), min_similarity, theme)
        self.episode_id, self.transcript_id, self.run_id, self.speaker_id = episode_id, transcript_id, run_id, speaker_id
        self.min_score, self.min_duration_ms, self.max_duration_ms = min_score, min_duration_ms, max_duration_ms
        self.exclude_id, self.exclude_episode_id = exclude_id, exclude_episode_id

    def filters(self, conn: psycopg.Connection, embed: EmbeddingService, *, sort: str | None = None,
                diversify: bool = False, cursor: str | None = None, facets: bool = False,
                ids: list[int] | None = None) -> clips_repo.ClipFilters:
        """Validate the selection and resolve what it needs: the query vector or liked centroid, theme members."""
        ranking = vector_repo.plan_clips(conn, embed, q=self.q, search=self.search, like=self.like,
                                         min_similarity=self.min_similarity, sort=sort, diversify=diversify,
                                         cursor=cursor, facets=facets)
        members = theme_repo.members(conn, embed, self.theme) if self.theme else None
        return clips_repo.ClipFilters(
            ids=ids or [], q=self.q, episode_ids=self.episode_id, transcript_ids=self.transcript_id,
            run_ids=self.run_id, speaker_ids=self.speaker_id, min_score=self.min_score, min_duration_ms=self.min_duration_ms,
            max_duration_ms=self.max_duration_ms, exclude_ids=self.exclude_id,
            exclude_episode_ids=self.exclude_episode_id, usage=self.usage, theme=self.theme, theme_clip_ids=members,
            ranking=ranking)


Clips = Annotated[ClipQuery, Depends()]


def _embed_early(db: Database, embed: EmbeddingService, q: str | None) -> None:
    """Embed a semantic query before the request takes its connection, so no pooled connection waits on the
    embedding server. plan_clips / plan_compilations then find it cached, or get the same error without asking
    again, and report it in their usual order. Not when no stored vector could be compared with it."""
    if not (q and len(q) <= vector_repo.MAX_QUERY_CHARS and embed.configured):
        return
    with db.connect() as conn:
        if not vector_repo.has_vectors(conn, embed.model_name):
            return
    try:
        embed.query(q)
    except EmbeddingUnavailable:
        pass


def clip_selection(request: Request, db: Db, query: Clips, embed: Embed) -> ClipQuery:
    """The clips' selection, its semantic query already embedded unless the request will be refused anyway
    (a 400 from plan_clips; other parameters are validated after this, so a bad one may cost a query)."""
    params = request.query_params
    planned = vector_repo.clip_query_errors(
        q=query.q, search=query.search, like=query.like, min_similarity=query.min_similarity, sort=params.get("sort"),
        diversify=(params.get("diversify") or "").lower() in ("1", "true", "yes", "on"), cursor=params.get("cursor"),
        facets=request.url.path.endswith("/facets"))
    if query.search in ("semantic", "hybrid") and not query.like and not planned.items:
        _embed_early(db, embed, query.q)
    return query


class CompilationSearch:
    def __init__(self, db: Db, embed: Embed, q: Annotated[str | None, Query(min_length=1)] = None,
                 search: Annotated[str | None, Query(pattern="^(keyword|semantic)$")] = None):
        self.q, self.search = q, search
        if search == "semantic":
            _embed_early(db, embed, q)


# Each is declared before the connection in its handler's parameters, so it runs (and embeds) first.
ClipSelection = Annotated[ClipQuery, Depends(clip_selection)]
CompilationQuery = Annotated[CompilationSearch, Depends()]
BATCH_ONLY_WITHOUT = ("limit", "cursor", "sort", "diversify", "max_per_episode")
SORT_PATTERN = "^(" + "|".join(k.replace("-", "\\-") for k in clips_repo.SORTS) + ")$"


@router.get("/clips")
def list_clips(request: Request, query: ClipSelection, conn: Conn, base: Base, embed: Embed,
               id: Annotated[list[int], Query(max_length=200)] = [],
               max_per_episode: Annotated[int | None, Query(ge=1, le=200)] = None, diversify: bool = False,
               sort: Annotated[str | None, Query(pattern=SORT_PATTERN)] = None,
               view: Annotated[str, Query(pattern="^(full|compact)$")] = "full",
               include: Annotated[list[str], Query()] = [],
               limit: Annotated[int, Query(ge=1, le=COMPACT_LIMIT)] = 25, cursor: str | None = None):
    errors = ErrorList()
    if id:  # a batch fetch returns exactly those clips in the requested order: paging and ordering don't apply
        for name in BATCH_ONLY_WITHOUT:
            if name in request.query_params:
                errors.add("invalid_value", f"`{name}` doesn't apply to a batch fetch by `id`.", parameter=name)
    if limit > 200 and view != "compact":
        errors.add("invalid_value", f"`limit` above 200 needs `view=compact` (up to {COMPACT_LIMIT}).",
                   parameter="limit")
    if include and view == "compact":
        errors.add("invalid_value", "`view=compact` leaves out text and includes; drop `include`.",
                   parameter="include")
    errors.raise_if_any(400)
    included = parse_include(include, clips_repo.INCLUDES)
    filters = query.filters(conn, embed, sort=sort, diversify=diversify, cursor=cursor, ids=id)
    return page(*clips_repo.list_clips(conn, filters, base=base, sort=sort, include=included, limit=limit,
                                       cursor=cursor, max_per_episode=max_per_episode, diversify=diversify,
                                       view=view))


@router.get("/clips/facets")
def clip_facets(query: ClipSelection, conn: Conn, embed: Embed, group_by: Annotated[str, Query(pattern="^(episode|speaker|usage)$")],
                sort: Annotated[str, Query(pattern="^(-clips|-unused)$")] = "-clips",
                limit: Annotated[int, Query(ge=1, le=500)] = 50):
    return clips_repo.facets(conn, query.filters(conn, embed, facets=True), group_by=group_by, sort=sort,
                             limit=limit)


@router.get("/themes")
def list_themes(conn: Conn, embed: Embed, k: Annotated[int, Query(ge=2, le=theme_repo.MAX_K)] = theme_repo.DEFAULT_K,
                sort: Annotated[str, Query(pattern="^(-unused|-clips|-cohesion|coverage)$")] = "-unused"):
    return theme_repo.list_themes(conn, embed, k, sort)


@router.get("/clips/{clip_id}")
def get_clip(conn: Conn, base: Base, clip_id: int, include: Annotated[list[str], Query()] = []):
    return clips_repo.get_clip(conn, clip_id, parse_include(include, clips_repo.INCLUDES), base)


@router.get("/clips/{clip_id}/context")
def get_clip_context(conn: Conn, clip_id: int, before_words: Annotated[int, Query(ge=0, le=2000)] = 150,
                     after_words: Annotated[int, Query(ge=0, le=2000)] = 80):
    return clips_repo.clip_context(conn, clip_id, before_words, after_words)


@router.get("/clips/{clip_id}/appearances")
def list_clip_appearances(conn: Conn, clip_id: int, limit: Limit = 25, cursor: str | None = None):
    return page(*clips_repo.appearances(conn, clip_id, limit, cursor))


@router.get("/shares/{slug}")
def get_shared_clip(conn: Conn, base: Base, slug: str, include: Annotated[list[str], Query()] = []):
    return clips_repo.get_shared(conn, slug, parse_include(include, clips_repo.INCLUDES), base)


# --- feed (the TV page) ---------------------------------------------------------

FEED_MAX_COUNT = 24
FEED_MAX_RECENT = 50


@router.get("/feed")
def get_feed(query: ClipSelection, conn: Conn, base: Base, embed: Embed, settings: Settings,
             count: Annotated[int, Query(ge=1, le=FEED_MAX_COUNT)] = 8,
             recent: Annotated[list[int], Query(max_length=FEED_MAX_RECENT)] = []):
    if (query.search in ("semantic", "hybrid") or query.like) and query.min_similarity is None:
        raise bad_request([FieldError(
            "invalid_value", "Semantic, hybrid and `like` match every embedded clip; set `min_similarity` to say "
            "which clips belong in the feed.", parameter="min_similarity")])
    filters = query.filters(conn, embed)
    return feed_repo.batch(conn, filters, recent=recent, count=count, base=base,
                           weights=feed_picker.mix(settings["feed_mix"]))


class FeedPlay(Closed):
    clip_id: Annotated[int, Field(ge=1, le=2 ** 63 - 1)]


@router.post("/feed/plays")
def record_feed_play(conn: Conn, body: FeedPlay):
    played = feed_repo.record_play(conn, body.clip_id)
    if played is None:
        raise invalid([FieldError("clip_not_found", f"Clip {body.clip_id} doesn't exist.", pointer="/clip_id")])
    return played


# --- compilations, reviews and renders ------------------------------------------

@router.post("/segments/review")
def review_segments(conn: Conn, embed: Embed, body: ReviewRequest):
    resolved = comp_repo.resolve_items(conn, [seg.as_dict() for seg in body.segments], "segments")
    return review_repo.review_segments(conn, resolved, embed.model_name, body.target_duration_ms)


@router.get("/compilations")
def list_compilations(found: CompilationQuery, conn: Conn, embed: Embed,
                      min_similarity: Annotated[float | None, Query(ge=-1, le=1)] = None,
                      clip_id: int | None = None, limit: Limit = 25, cursor: str | None = None):
    ranking = vector_repo.plan_compilations(conn, embed, q=found.q, search=found.search,
                                            min_similarity=min_similarity)
    return page(*comp_repo.list_compilations(conn, q=found.q, clip_id=clip_id, limit=limit, cursor=cursor,
                                             ranking=ranking))


def _write(request: Request, work) -> Response:
    with request.app.state.db.connect() as conn:
        status, payload, headers = work(conn)
    return respond(status, payload, headers)


@router.post("/compilations")
def create_compilation(request: Request, body: NewCompilation, dry_run: bool = False):
    data = {"title": body.title, "items": [seg.as_dict() for seg in body.items], "metadata": body.metadata or {}}

    def work(conn):
        status, payload = comp_repo.create_compilation(conn, data, dry_run)
        return status, payload, ({"Location": f"{PREFIX}/revisions/{payload['id']}"} if status == 201 else {})

    return _write(request, work)


@router.get("/compilations/{compilation_id}")
def get_compilation(conn: Conn, compilation_id: int):
    return comp_repo.get_compilation(conn, compilation_id)


@router.get("/compilations/{compilation_id}/revisions")
def list_revisions(conn: Conn, compilation_id: int, limit: Limit = 25, cursor: str | None = None):
    return page(*comp_repo.list_revisions(conn, compilation_id, limit, cursor))


@router.post("/compilations/{compilation_id}/revisions")
def create_revision(request: Request, compilation_id: int, body: NewRevision, dry_run: bool = False):
    data = {"based_on": body.based_on, "items": [seg.as_dict() for seg in body.items],
            "metadata": body.metadata or {}}

    def work(conn):
        status, payload = comp_repo.create_revision(conn, compilation_id, data, dry_run)
        return status, payload, ({"Location": f"{PREFIX}/revisions/{payload['id']}"} if status == 201 else {})

    return _write(request, work)


@router.get("/compilations/{compilation_id}/revisions/{number}")
def redirect_to_revision(request: Request, conn: Conn, compilation_id: int,
                         number: Annotated[str, Path(pattern=r"^([1-9]\d*|latest)$")],
                         include: Annotated[list[str], Query()] = []):  # passed on to the revision, which checks it
    revision_id = comp_repo.revision_id_for(conn, compilation_id, number if number == "latest" else int(number))
    location = f"{PREFIX}/revisions/{revision_id}" + (f"?{request.url.query}" if request.url.query else "")
    cache = "no-store" if number == "latest" else "max-age=86400"
    return Response(status_code=307, headers={"Location": location, "Cache-Control": cache})


def _etag_matches(header: str, etag: str) -> bool:
    """If-None-Match uses weak comparison (RFC 9110 §13.1.2): `W/` is ignored, and `*` matches anything."""
    tags = [t.strip().removeprefix("W/") for t in header.split(",")]
    return "*" in tags or etag.removeprefix("W/") in tags


@router.get("/revisions/{revision_id}")
def get_revision(conn: Conn, revision_id: int, include: Annotated[list[str], Query()] = [],
                 if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None):
    revision = comp_repo.get_revision(conn, revision_id, parse_include(include, {"captions", "speakers"}))
    body = json.dumps(revision, ensure_ascii=False, separators=(",", ":")).encode()
    etag = 'W/"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    if if_none_match and _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="application/json", headers={"ETag": etag, "Cache-Control": "no-cache"})


@router.get("/revisions/{revision_id}/diff")
def diff_revision(conn: Conn, revision_id: int, against: int | None = None):
    return comp_repo.diff_revisions(conn, revision_id, against)


@router.get("/revisions/{revision_id}/review")
def review_revision(conn: Conn, embed: Embed, revision_id: int,
                    target_duration_ms: Annotated[int | None, Query(ge=1)] = None):
    return review_repo.review_revision(conn, revision_id, embed.model_name, target_duration_ms)


@router.get("/revisions/{revision_id}/renders")
def list_renders(conn: Conn, revision_id: int, limit: Limit = 25, cursor: str | None = None):
    return page(*render_repo.list_renders(conn, revision_id, limit, cursor))


@router.post("/revisions/{revision_id}/renders")
def record_render(request: Request, revision_id: int, body: NewRender):
    data = body.model_dump(mode="json", exclude_unset=True)
    return _write(request, lambda conn: (201, render_repo.record_render(conn, revision_id, data), {}))


# --- subscriptions ------------------------------------------------------------

class NewSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=300)
    uploads: bool = True
    streams: bool = False
    autoclip: bool | None = None  # omitted: off for a new subscription, unchanged for an existing one


class SubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uploads: bool | None = None
    streams: bool | None = None
    autoclip: bool | None = None


EXPORT_FORMAT = "yolt-subscriptions"
MAX_IMPORT = 5000


class ImportedSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: str = Field(max_length=100)  # a UC… id: checked with the rest, for a readable message
    name: str | None = Field(default=None, max_length=300)  # empty: the channel id, until its first check
    handle: str | None = Field(default=None, max_length=31)
    url: str | None = Field(default=None, max_length=300)  # as exported; the link is rebuilt from channel_id
    uploads: bool = True
    streams: bool = False
    autoclip: bool = False


class SubscriptionImport(BaseModel):
    """An export file (GET /subscriptions/export), as it is or written by hand."""
    model_config = ConfigDict(extra="forbid")
    format: Literal["yolt-subscriptions"] = EXPORT_FORMAT
    version: Literal[1] = 1
    exported_at: str | None = Field(default=None, max_length=40)
    subscriptions: list[ImportedSubscription] = Field(max_length=MAX_IMPORT)


def _check_hours(db: Database) -> int:
    return subscriptions.schedule(db.settings())[0]


def _wake_collector(request: Request) -> None:
    request.app.state.worker.subscriptions.wake()


@router.get("/subscriptions")
def list_subscriptions(db: Db, limit: Limit = 25, cursor: str | None = None):
    hours = _check_hours(db)
    with db.connect() as conn:
        return page(*subs_repo.list_subscriptions(conn, hours=hours, limit=limit, cursor=cursor))


@router.post("/subscriptions")
def create_subscription(request: Request, db: Db, body: NewSubscription):
    errors = ErrorList()
    path = video_id = None
    try:
        path = youtube.channel_path(body.url)
    except youtube.YouTubeError:
        try:  # a link to one of its videos: the channel it's on
            video_id = youtube.parse_video_id(body.url)
        except youtube.YouTubeError:
            errors.add("not_youtube", "Enter a YouTube @handle, channel URL, UC… channel id, or a link to one of "
                       "the channel's videos.", at="/url")
    if not (body.uploads or body.streams):
        errors.add("nothing_followed", "Follow uploads, finished livestreams, or both.", at="/uploads")
    errors.raise_if_any()
    if video_id:  # the video's details name its channel (and are cached, as for a run)
        channel_id = _video_info(request, video_id, at="/url").get("channel_id")
        if not (isinstance(channel_id, str) and youtube.CHANNEL_ID_RE.match(channel_id)):
            raise invalid([FieldError("channel_unavailable", "YouTube didn't say which channel that video is on.",
                                      pointer="/url")])
        path = f"/channel/{channel_id}"
    try:  # yt-dlp takes a second or so: no connection held meanwhile
        channel = youtube.channel_details(youtube.fetch_channel(path))
    except youtube.YouTubeError as error:
        raise invalid([FieldError("channel_unavailable", str(error), pointer="/url")]) from error
    with db.connect() as conn:
        subscription_id, created = subs_repo.subscribe(conn, channel, uploads=body.uploads, streams=body.streams,
                                                       autoclip=body.autoclip)
    _wake_collector(request)
    hours = _check_hours(db)
    with db.connect() as conn:
        subscription = subs_repo.get_subscription(conn, subscription_id, hours=hours)
    if not created:
        return respond(200, subscription)
    return respond(201, subscription, {"Location": f"{PREFIX}/subscriptions/{subscription_id}"})


@router.get("/subscriptions/videos")
def list_subscription_videos(conn: Conn, kind: Literal["upload", "stream"] | None = None,
                             podcast_id: Annotated[list[int], Query()] = [], clipped: bool | None = None,
                             limit: Limit = 25, cursor: str | None = None):
    return page(*subs_repo.list_videos(conn, kind=kind, podcast_ids=podcast_id, clipped=clipped, limit=limit,
                                       cursor=cursor))


@router.get("/subscriptions/export")
def export_subscriptions(conn: Conn):
    now = datetime.now(timezone.utc)
    body = {"format": EXPORT_FORMAT, "version": 1, "exported_at": now.isoformat(timespec="seconds"),
            "subscriptions": subs_repo.export_subscriptions(conn)}
    return Response(json.dumps(body, indent=2, ensure_ascii=False) + "\n", media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="yolt-subscriptions-{now.astimezone():%Y-%m-%d}.json"'})


@router.post("/subscriptions/import")
def import_subscriptions(request: Request, db: Db, body: SubscriptionImport):
    errors = ErrorList()
    channels: dict[str, dict] = {}  # by channel id: a channel listed twice is imported as first listed
    for i, sub in enumerate(body.subscriptions):
        if not youtube.CHANNEL_ID_RE.match(sub.channel_id):
            errors.add("invalid_value", "Not a YouTube channel id (UC and 22 more letters, digits, _ or -).",
                       at=f"/subscriptions/{i}/channel_id")
        handle = (sub.handle or "").strip() or None
        if handle and not (handle.startswith("@") and youtube.HANDLE_RE.match(handle[1:])):
            errors.add("invalid_value", "A handle is @ and 3 to 30 letters, digits, _ - or .",
                       at=f"/subscriptions/{i}/handle")
        if not (sub.uploads or sub.streams):
            errors.add("nothing_followed", "Follow uploads, finished livestreams, or both.",
                       at=f"/subscriptions/{i}/uploads")
        channels.setdefault(sub.channel_id, {
            "channel_id": sub.channel_id, "name": (sub.name or "").strip() or sub.channel_id, "handle": handle,
            "uploads": sub.uploads, "streams": sub.streams, "autoclip": sub.autoclip})
    errors.raise_if_any()
    with db.connect() as conn:  # all or nothing
        created, existing = subs_repo.import_subscriptions(conn, list(channels.values()))
    if created:
        _wake_collector(request)
    return {"created": created, "already_subscribed": existing}


@router.post("/subscriptions/check")
def check_subscriptions(request: Request, db: Db):
    with db.connect() as conn:
        pending = subs_repo.request_checks(conn)
    _wake_collector(request)
    return respond(202, {"pending": pending})


@router.get("/subscriptions/{subscription_id}")
def get_subscription(db: Db, subscription_id: int):
    hours = _check_hours(db)
    with db.connect() as conn:
        return subs_repo.get_subscription(conn, subscription_id, hours=hours)


@router.post("/subscriptions/{subscription_id}/check")
def check_subscription(request: Request, db: Db, subscription_id: int):
    hours = _check_hours(db)
    with db.connect() as conn:
        subs_repo.request_check(conn, subscription_id)
        subscription = subs_repo.get_subscription(conn, subscription_id, hours=hours)  # before the check can end
    _wake_collector(request)
    return respond(202, subscription)


@router.patch("/subscriptions/{subscription_id}")
def update_subscription(request: Request, db: Db, subscription_id: int, body: SubscriptionUpdate):
    with db.connect() as conn:
        subs_repo.update(conn, subscription_id, uploads=body.uploads, streams=body.streams, autoclip=body.autoclip)
    _wake_collector(request)
    hours = _check_hours(db)
    with db.connect() as conn:
        return subs_repo.get_subscription(conn, subscription_id, hours=hours)


@router.delete("/subscriptions/{subscription_id}")
def delete_subscription(db: Db, subscription_id: int):
    with db.connect() as conn:
        subs_repo.unsubscribe(conn, subscription_id)
    return Response(status_code=204)


# --- settings -----------------------------------------------------------------

class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parakeet_url: str | None = None
    parakeet_model: str | None = None
    youtube_captions: Literal["any", "manual", "off"] | None = None
    highlightr_url: str | None = None
    llm_endpoint: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    embed_url: str | None = None
    embed_model: str | None = None
    min_clip_seconds: str | None = None
    max_clip_seconds: str | None = None
    max_clips: str | None = None
    candidates_per_window: str | None = None
    clip_instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS)
    subscription_check_hours: str | None = None
    subscription_min_minutes: str | None = None
    labs_disabled: str | None = Field(default=None, max_length=1000)
    feed_mix: str | None = Field(default=None, max_length=100)


class ServiceTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Literal["parakeet", "highlightr", "llm", "embeddings"]
    url: str
    model: str | None = None
    api_key: str | None = None


def settings_view(db: Database) -> dict:
    values = db.settings()
    secrets = [k for k, s in SETTINGS.items() if s.secret]
    return {
        "values": {k: ("" if k in secrets else v) for k, v in values.items()},
        "defaults": {k: db.defaults[k] for k, s in SETTINGS.items() if not s.secret},
        "secrets_configured": [k for k in secrets if values[k]],
        "env_overrides": sorted(db.env_overrides()),
    }


def _settings_errors(update: dict[str, str], merged: dict[str, str]) -> ErrorList:
    """Problems with the non-empty values in `update`; an empty one means the default."""
    errors = ErrorList()
    given = {k: v for k, v in update.items() if v}
    for key, label in (("parakeet_url", "Parakeet"), ("highlightr_url", "Highlightr"),
                       ("llm_endpoint", "LLM endpoint"), ("embed_url", "Embeddings")):
        if key in given:
            try:
                services.normalize_base_url(given[key], label)
            except services.ServiceError as error:
                errors.add("invalid_value", str(error), at=f"/{key}")
    def whole(key: str, low: int, high: int | None = None) -> bool:
        """Whether given[key] is a whole number from `low` to `high`."""
        n = _whole(given[key])
        return n is not None and n >= low and (high is None or n <= high)

    for key in ("min_clip_seconds", "max_clip_seconds"):
        if key in given and not whole(key, 1):
            errors.add("invalid_value", "Clip lengths must be whole seconds above 0.", at=f"/{key}")
    if "max_clips" in given and not whole("max_clips", 1):
        errors.add("invalid_value", "Max clips must be a whole number above 0.", at="/max_clips")
    if "candidates_per_window" in given and not whole("candidates_per_window", 1, MAX_CANDIDATES):
        errors.add("invalid_value", f"Candidates per window must be a whole number from 1 to {MAX_CANDIDATES}.",
                   at="/candidates_per_window")
    if "subscription_check_hours" in given and not whole("subscription_check_hours", 0, MAX_CHECK_HOURS):
        errors.add("invalid_value", f"Check subscriptions every 0 to {MAX_CHECK_HOURS} hours (0: only when asked).",
                   at="/subscription_check_hours")
    if "subscription_min_minutes" in given and not whole("subscription_min_minutes", 0, MAX_MIN_MINUTES):
        errors.add("invalid_value", f"The shortest video kept must be 0 to {MAX_MIN_MINUTES} minutes.",
                   at="/subscription_min_minutes")
    if "labs_disabled" in given and (unknown := labs.unknown(given["labs_disabled"])):
        errors.add("invalid_value", f"Not a lab: {', '.join(unknown)}. Labs: {', '.join(labs.BY_ID)}.",
                   at="/labs_disabled")
    if "feed_mix" in given and feed_picker.parse_mix(given["feed_mix"]) is None:
        errors.add("invalid_value", f"The TV mix is {len(feed_picker.TIER_NAMES)} whole numbers from 0 to "
                   f"{feed_picker.MAX_WEIGHT}, separated by commas (last 3, 7, 30 and 90 days, then all time), "
                   "and at least one above 0.", at="/feed_mix")
    shortest, longest = _whole(merged["min_clip_seconds"]), _whole(merged["max_clip_seconds"])
    if not errors.items and shortest is not None and longest is not None and shortest > longest:
        errors.add("invalid_value", "Minimum clip length can't exceed the maximum.", at="/min_clip_seconds")
    return errors


def _whole(value: str) -> int | None:
    """`value` as a whole number if it's ASCII digits, else None. (`str.isdigit` alone also accepts "²" and "①",
    which `int` then refuses, and `int` refuses more than 4300 digits.)"""
    if not (value.isascii() and value.isdigit()):
        return None
    try:
        return int(value)
    except ValueError:
        return None


@router.get("/settings")
def get_settings(db: Db):
    return settings_view(db)


@router.put("/settings")
def update_settings(request: Request, db: Db, body: SettingsUpdate):
    update = {k: v.strip() for k, v in body.model_dump(exclude_none=True).items()}
    locked = sorted(db.env_overrides() & update.keys())
    if locked:
        raise ApiProblem(409, "locked", "Set by an environment variable", errors=[
            FieldError("locked", f"`{k}` is set by {SETTINGS[k].env} and can't be changed here.", pointer=f"/{k}")
            for k in locked])
    defaults = {k: v or db.defaults[k] for k, v in update.items()}
    _settings_errors(update, db.settings() | defaults).raise_if_any()
    for key in ("parakeet_url", "highlightr_url", "llm_endpoint", "embed_url"):
        if update.get(key):
            update[key] = update[key].rstrip("/")
    before = db.settings()
    db.save_settings(update)
    after = db.settings()
    if any(before[k] != after[k] for k in ("embed_url", "embed_model")):
        request.app.state.worker.embeddings_changed()
    if before["subscription_check_hours"] != after["subscription_check_hours"]:
        _wake_collector(request)  # a shorter interval may make checks due now
    return settings_view(db)


def _same_llm(url: str, saved: str) -> bool:
    """Whether two LLM endpoint settings name the same chat completions URL."""
    try:
        return bool(saved.strip()) and services.chat_completions_url(url) == services.chat_completions_url(saved)
    except services.ServiceError:
        return False


@router.post("/settings/test")
def test_service(request: Request, db: Db, embed: Embed, body: ServiceTest):
    if body.service == "embeddings":
        model = (db.settings()["embed_model"] if body.model is None else body.model)
        try:
            return {"ok": True, "message": embed.check(body.url, model)}
        except EmbeddingUnavailable as error:
            return {"ok": False, "message": str(error)}
    try:
        with request.app.state.client_factory() as client:
            if body.service == "llm":
                saved = db.settings()
                model = (saved["llm_model"] if body.model is None else body.model).strip()
                if body.api_key is not None:
                    key = body.api_key.strip()
                else:  # GET /settings never shows the saved key, so it's only ever sent where it's saved for
                    key = saved["llm_api_key"].strip() if _same_llm(body.url, saved["llm_endpoint"]) else ""
                return {"ok": True, "message": services.check_llm(client, body.url, model, key)}
            label = "Parakeet" if body.service == "parakeet" else "Highlightr"
            return {"ok": True, "message": services.check_health(client, body.url, label)}
    except services.ServiceError as error:
        return {"ok": False, "message": str(error)}


# --- clearing the catalogue ------------------------------------------------------

class ClearCatalogue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal["clear"]  # so a stray request can't delete everything


@router.post("/catalogue/clear")
def clear_catalogue(request: Request, db: Db, body: ClearCatalogue):
    try:
        counts = db.clear_catalogue()
    except psycopg.errors.LockNotAvailable as error:
        raise ApiProblem(409, "busy", "Yolt is busy writing to the catalogue; try again in a moment") from error
    thumbnails = request.app.state.worker.clear_files()
    request.app.state.worker.wake()  # whatever it was on is gone
    return {**counts, "thumbnails": thumbnails}


# --- install ------------------------------------------------------------------

def install(app: FastAPI) -> None:
    """Add the router, the labs' router (yolt/labs), and their problem+json error handling to the app."""
    from .labs.routes import router as labs_router  # imports this module, so not at the top

    app.include_router(router)
    app.include_router(labs_router)

    @app.exception_handler(ApiProblem)
    async def _problem(_: Request, exc: ApiProblem):
        return respond(exc.status, exc.body(), exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        problem = validation_problem(exc)
        return respond(problem.status, problem.body())

    @app.exception_handler(404)
    async def _not_found(_: Request, __):
        return respond(404, ApiProblem(404, "not-found", "No such resource").body())

    @app.exception_handler(405)
    async def _method(request: Request, exc):
        # Keep the HTTPException's headers. Its `Allow` names the first matching route's methods only; add the
        # other routes declared for that same path (GET and POST /compilations are two), not every template
        # the path happens to fit (`/subscriptions/check` isn't a `/subscriptions/{subscription_id}`).
        matching = [route for route in (*router.routes, *labs_router.routes)
                    if route.matches(request.scope)[0] != Match.NONE]
        methods = sorted({m for route in matching if route.path == matching[0].path
                          for m in getattr(route, "methods", None) or ()})
        headers = {**(getattr(exc, "headers", None) or {}), **({"Allow": ", ".join(methods)} if methods else {})}
        return respond(405, ApiProblem(405, "method-not-allowed", "Method not allowed").body(), headers)

    @app.exception_handler(Exception)
    async def _crash(_: Request, __: Exception):
        return respond(500, ApiProblem(500, "internal", "Internal server error").body(), SECURITY_HEADERS)
