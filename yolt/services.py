"""Clients for the Parakeet (transcription) and Highlightr (highlights) services.

Both are the same services RipeClips uses; see ripeclips/parakeet and
ripeclips/highlightr for deployment.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

# Transcribing or ranking a long episode can take many minutes; only the
# connection itself is bounded.
# Uploading and transcribing a long episode can take a while, but not forever: a service that
# accepts the request and never answers must not hold the only worker.
LONG_TIMEOUT = httpx.Timeout(connect=10.0, read=2 * 3600.0, write=30 * 60.0, pool=60.0)
HIGHLIGHTR_DEADLINE = 6 * 3600.0  # a Highlightr job still running after this is given up on
POLL_SECONDS = 3.0
# While waiting on a job, Highlightr being unreachable (a restart, a proxy hiccup) is retried with
# backoff, and only gives up the job after this long without an answer.
OUTAGE_SECONDS = 5 * 60.0
RETRY_SECONDS, MAX_RETRY_SECONDS = 1.0, 30.0
TRANSIENT_STATUS = {502, 503, 504}


class ServiceError(RuntimeError):
    pass


class Interrupted(Exception):
    """The worker is stopping; the run should be resumed, not marked failed."""


def normalize_base_url(value: str, label: str) -> str:
    value = (value or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ServiceError(f"{label} URL must be an http(s) URL.")
    return value


def _raise_for_status(response: httpx.Response, label: str) -> None:
    if response.is_success:
        return
    detail = response.text.strip()[:500]
    raise ServiceError(f"{label} returned HTTP {response.status_code}: {detail}")


def _request(label: str, base_url: str, call: Callable[[], httpx.Response]) -> httpx.Response:
    try:
        response = call()
    except httpx.TimeoutException as error:
        raise ServiceError(f"{label} at {base_url} timed out.") from error
    except httpx.HTTPError as error:
        raise ServiceError(f"Could not reach {label} at {base_url}: {error}") from error
    _raise_for_status(response, label)
    return response


def check_health(client: httpx.Client, base_url: str, label: str) -> str:
    base_url = normalize_base_url(base_url, label)
    _request(label, base_url, lambda: client.get(f"{base_url}/health", timeout=5))
    return f"{label} is reachable."


def chat_completions_url(endpoint: str) -> str:
    """Accept a versioned base URL or the full URL, like Highlightr does."""
    endpoint = normalize_base_url(endpoint, "LLM endpoint")
    return endpoint if endpoint.endswith("/chat/completions") else f"{endpoint}/chat/completions"


def check_llm(client: httpx.Client, endpoint: str, model: str = "", api_key: str = "") -> str:
    """Send a tiny chat completion to an OpenAI-compatible endpoint."""
    if not (endpoint or "").strip():
        raise ServiceError("No LLM endpoint set, so Highlightr will use the LLM it was started with.")
    url = chat_completions_url(endpoint)
    payload: dict = {"messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 16}
    if model:
        payload["model"] = model
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = _request(
        "LLM", url,
        lambda: client.post(url, json=payload, headers=headers, timeout=httpx.Timeout(60.0, connect=5.0)),
    )
    try:
        data = response.json()
    except ValueError as error:
        raise ServiceError("LLM returned invalid JSON.") from error
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        raise ServiceError("LLM response has no choices; is this an OpenAI-compatible chat endpoint?")
    served = data.get("model") or model
    return f"LLM replied{f' (model {served})' if served else ''}."


def transcribe(client: httpx.Client, base_url: str, model: str, audio: Path) -> dict:
    """POST audio to Parakeet's OpenAI-compatible endpoint; return verbose_json."""
    base_url = normalize_base_url(base_url, "Parakeet")
    with audio.open("rb") as handle:
        response = _request(
            "Parakeet",
            base_url,
            lambda: client.post(
                f"{base_url}/v1/audio/transcriptions",
                data={
                    "model": model,
                    "response_format": "verbose_json",
                    "timestamp_granularities": "word",
                },
                files={"file": (audio.name, handle, "audio/mpeg")},
                timeout=LONG_TIMEOUT,
            ),
        )
    try:
        data = response.json()
    except ValueError as error:
        raise ServiceError("Parakeet returned invalid JSON.") from error
    if not isinstance(data, dict):
        raise ServiceError("Parakeet returned an unexpected response.")
    return data


def _get_through_outages(
    client: httpx.Client, base_url: str, url: str, timeout: float, should_stop: Callable[[], bool],
    outage_seconds: float,
) -> httpx.Response:
    """GET from Highlightr, retrying connection errors, timeouts and 502/503/504 with backoff until
    they've gone on for `outage_seconds`. Other answers (404 included) are returned as they are.
    Raises Interrupted if `should_stop()` turns true meanwhile."""
    failing_since, delay = None, RETRY_SECONDS
    while True:
        try:
            response = client.get(url, timeout=timeout)
            if response.status_code not in TRANSIENT_STATUS:
                return response
            problem = f"HTTP {response.status_code}: {response.text.strip()[:200]}"
        except httpx.TransportError as error:  # can't connect, timed out, connection dropped
            problem = str(error) or type(error).__name__
        now = time.monotonic()
        failing_since = now if failing_since is None else failing_since
        if now - failing_since >= outage_seconds:
            raise ServiceError(f"Highlightr at {base_url} didn't answer for {outage_seconds / 60:g} minutes "
                               f"while running a job: {problem}")
        if should_stop():
            raise Interrupted
        time.sleep(delay)
        delay = min(delay * 2, MAX_RETRY_SECONDS)


def identify_highlights(
    client: httpx.Client,
    base_url: str,
    srt_text: str,
    fields: dict[str, str],
    on_progress: Callable[[int, int, str], None] = lambda done, total, stage: None,
    poll_seconds: float = POLL_SECONDS,
    should_stop: Callable[[], bool] = lambda: False,
    on_job: Callable[[str], None] = lambda job_id: None,
    deadline_seconds: float = HIGHLIGHTR_DEADLINE,
    job_id: str | None = None,
    outage_seconds: float = OUTAGE_SECONDS,
) -> dict:
    """Run a Highlightr async SRT job and return its result payload.

    `job_id` is a job submitted earlier with these same inputs (the run was retried, or Yolt restarted
    while waiting): it's picked up again if Highlightr still has it and it didn't fail; otherwise
    (Highlightr answers 404 once it restarted or the job expired) a new job is submitted.

    Raises Interrupted when `should_stop()` turns true while waiting, and ServiceError if the
    job is still running after `deadline_seconds` or Highlightr is unreachable for `outage_seconds`."""
    base_url = normalize_base_url(base_url, "Highlightr")
    jobs = f"{base_url}/v2/highlights/jobs"

    def get(url: str, timeout: float) -> httpx.Response:
        return _get_through_outages(client, base_url, url, timeout, should_stop, outage_seconds)

    status = None
    if job_id:
        response = get(f"{jobs}/{job_id}", 30)
        if response.status_code != 404:
            _raise_for_status(response, "Highlightr")
            status = response.json()
            if status.get("status") == "failed":
                status = None  # don't fail again on the attempt that failed; try once more
    if status is None:
        response = _request(
            "Highlightr",
            base_url,
            lambda: client.post(
                f"{base_url}/v2/highlights/srt-jobs",
                data=fields,
                files={"file": ("transcript.srt", srt_text.encode(), "application/x-subrip")},
                timeout=httpx.Timeout(60.0, connect=10.0),
            ),
        )
        status = response.json()
        job_id = status["job_id"]
    on_job(job_id)
    deadline = time.monotonic() + deadline_seconds
    while True:
        progress = status.get("progress") or {}
        on_progress(progress.get("windows_done", 0), progress.get("windows_total", 0), status.get("stage") or "")
        if status.get("status") == "completed":
            break
        if status.get("status") == "failed":
            raise ServiceError(f"Highlightr job failed: {status.get('error') or 'unknown error'}")
        if should_stop():
            raise Interrupted
        if time.monotonic() > deadline:
            raise ServiceError(f"Highlightr job {job_id} was still running after {deadline_seconds / 3600:g} hours.")
        time.sleep(poll_seconds)
        response = get(f"{jobs}/{job_id}", 30)
        if response.status_code == 404:
            raise ServiceError(f"Highlightr no longer has job {job_id} (did it restart?); retry the run to start over.")
        _raise_for_status(response, "Highlightr")
        status = response.json()
    response = get(f"{jobs}/{job_id}/result", 60)
    _raise_for_status(response, "Highlightr")
    return response.json()
