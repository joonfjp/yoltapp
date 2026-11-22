"""Fake Parakeet / Highlightr / LLM / YouTube, shared by the API and storage tests."""

import json
import threading
import time

import httpx
from fastapi.testclient import TestClient

from yolt import youtube
from tests.support.contract import ContractClient
from tests.support.embedder import FakeEmbeddingServer
from yolt.server import create_app

VIDEO = "dQw4w9WgXcQ"
CHANNEL_ID = "UC" + "p" * 22
SPOKEN = "So here's the thing. Nobody saw it coming, honestly. And then everything changed forever."


class FakeServices:
    def __init__(self):
        self.highlightr_fields = None
        self.fail_highlightr = False
        self.transcribe_calls = 0
        self.llm_requests = []
        self.embeddings = FakeEmbeddingServer()
        self.speaker_requests = []
        self.speakers_down = False
        self.people = {"hosts": ["Ada Host"], "guests": ["Gus Guest", "ada host"]}
        self.hold_highlightr = threading.Event()  # while set, Highlightr's job reports it's still running
        self.job_polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v1/embeddings"):
            return self.embeddings.handler(request)
        if path.endswith("/chat/completions"):
            self.llm_requests.append(request)
            if request.url.host == "down":
                return httpx.Response(401, text="bad key")
            body = json.loads(request.content)
            if "response_format" in body:  # speaker inference
                self.speaker_requests.append(body)
                if self.speakers_down:
                    return httpx.Response(503, text="LLM busy")
                return httpx.Response(200, json={"model": "zion", "choices": [{"message": {
                    "content": "```json\n" + json.dumps(self.people) + "\n```"}}]})
            return httpx.Response(200, json={"model": "zion", "choices": [{"message": {"content": "OK"}}]})
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/audio/transcriptions":
            self.transcribe_calls += 1
            body = request.read()
            assert b'name="timestamp_granularities"' in body and b"verbose_json" in body
            tokens = SPOKEN.replace(",", "").replace(".", "").replace("'", " '").split()
            words = [{"word": t, "start": i * 1.0, "end": i * 1.0 + 0.8} for i, t in enumerate(tokens)]
            return httpx.Response(200, json={"text": SPOKEN, "words": words, "duration": 20.0, "language": "en"})
        if path == "/v2/highlights/srt-jobs":
            body = request.read().decode()
            self.highlightr_fields = body
            if self.fail_highlightr:
                return httpx.Response(503, text="LLM unavailable")
            return httpx.Response(202, json={"job_id": "j1", "status": "queued", "stage": "queued",
                                             "progress": {"windows_done": 0, "windows_total": 2}})
        if path == "/v2/highlights/jobs/j1":
            self.job_polls += 1
            if self.hold_highlightr.is_set():
                time.sleep(0.01)
                return httpx.Response(200, json={"job_id": "j1", "status": "running", "stage": "identifying",
                                                 "progress": {"windows_done": 1, "windows_total": 2}})
            return httpx.Response(200, json={"job_id": "j1", "status": "completed", "stage": "done",
                                             "progress": {"windows_done": 2, "windows_total": 2}})
        if path == "/v2/highlights/jobs/j1/result":
            return httpx.Response(200, json={"clips": [
                {"rank": 2, "start_seconds": 5.0, "end_seconds": 9.9, "title": "Second",
                 "summary": "s2", "scores": {"overall": 0.7}},
                {"rank": 1, "clip_id": "clip_001", "start_seconds": 0.0, "end_seconds": 4.85,
                 "best_headline": "The Thing", "best_description": "d1", "best_quotes": ["here's the thing"],
                 "scores": {"overall": 0.9}, "category": {"primary": "insight"}},
            ]})
        return httpx.Response(404)


def entry(video_id, seconds, *, live_status=None, duration=0, days_ago=1, **extra):
    """One entry of a flat channel-tab listing, as yt-dlp gives it."""
    return {"id": video_id, "title": f"Video {video_id}", "duration": seconds if duration == 0 else duration,
            "live_status": live_status, "timestamp": int(time.time()) - days_ago * 86400, **extra}


def make_fake(monkeypatch, tmp_path):
    services = FakeServices()
    downloads = []

    services.info_extra = {}  # e.g. subtitles / automatic_captions
    services.captions = None  # json3 payload, or an exception to raise

    services.info_calls = 0
    services.info_error = None

    def fetch_info(video_id):
        services.info_calls += 1
        if services.info_error:
            raise services.info_error
        return {"id": video_id, "title": "Great Episode", "channel": "Some Pod",
                "channel_url": "https://www.youtube.com/@pod", "upload_date": "20261101", "duration": 20,
                **services.info_extra}

    services.hold_downloads = set()  # video ids whose download keeps reporting progress until released

    def download_audio(video_id, workdir, on_progress=lambda f: None, check=lambda: None):
        downloads.append(video_id)
        while video_id in services.hold_downloads:
            on_progress(0.5)
            time.sleep(0.01)
        on_progress(1.0)
        path = workdir / f"{video_id}.mp3"
        path.write_bytes(b"ID3fake")
        return path

    def fetch_captions(track):
        services.fetched_track = track
        if isinstance(services.captions, Exception):
            raise services.captions
        return services.captions

    # Channels for subscriptions: what youtube.fetch_channel returns for each, and their tabs' entries
    # (a missing tab raises NoSuchTab, like a channel that has never streamed).
    services.channels = [{"channel_id": CHANNEL_ID, "channel": "Some Pod", "uploader_id": "@pod",
                          "channel_url": f"https://www.youtube.com/channel/{CHANNEL_ID}"}]
    services.tabs = {(CHANNEL_ID, "videos"): [entry("upl00000001", 3600), entry("upl00000002", 600),
                                              entry("upl00000003", 2700)],
                     (CHANNEL_ID, "streams"): [entry("str00000001", 7200, live_status="is_upcoming", duration=None),
                                               entry("str00000002", 5400, live_status="was_live")]}
    services.channel_calls = []
    services.channel_error = None  # an exception every channel request raises

    def fetch_channel(path, tab=None, *, limit=1):
        services.channel_calls.append((path, tab, limit))
        if services.channel_error:
            raise services.channel_error
        key = path.rsplit("/", 1)[-1]
        channel = next((c for c in services.channels if key in (c["uploader_id"], c["channel_id"])), None)
        if channel is None:
            raise youtube.YouTubeError("No YouTube channel matches that.")
        entries = []
        if tab:
            listed = services.tabs.get((channel["channel_id"], tab))
            if listed is None:
                raise youtube.NoSuchTab(f"This channel does not have a {tab} tab")
            entries = listed[:limit]
        return {**channel, "id": channel["channel_id"], "entries": entries}

    monkeypatch.setattr(youtube, "fetch_channel", fetch_channel)
    monkeypatch.setattr(youtube, "fetch_info", fetch_info)
    monkeypatch.setattr(youtube, "download_audio", download_audio)
    monkeypatch.setattr(youtube, "fetch_captions", fetch_captions)
    services.downloads = downloads
    return services


def make_client(fake, tmp_path, database_url):
    app = create_app(
        tmp_path,
        database_url=database_url,
        allowed_hosts=["testserver"],
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)),
        poll_seconds=0,
        subscription_pause_seconds=0,
    )
    with TestClient(app) as test_client:
        yield ContractClient(test_client)


def wait_for(client, run_id, status, timeout=10) -> dict:
    """Poll a run (with its clips) until it reaches `status`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}?include=clips").json()
        if run["status"] == status:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run stuck in {run['status']}: {run}")


def submit(client, url: str = VIDEO, **body) -> int:
    """POST /runs and return the run id."""
    response = client.post("/api/v1/runs", json={"url": url, **body})
    assert response.status_code in (200, 202), response.text
    return response.json()["id"]
