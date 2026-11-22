import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from yolt import youtube


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PL123",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=abc",
    "youtu.be/dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "dQw4w9WgXcQ",
    "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ",
])
def test_parse_video_id_accepts_youtube_links(url):
    assert youtube.parse_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    "",
    "https://vimeo.com/123456",
    "https://evil.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
    "file:///etc/passwd",
    "https://www.youtube.com/watch?v=short",
    "https://www.youtube.com/@channel",
    "https://www.youtube.com/playlist?list=PL123",
])
def test_parse_video_id_rejects_everything_else(url):
    with pytest.raises(youtube.YouTubeError):
        youtube.parse_video_id(url)


def test_metadata_prefers_timestamp():
    meta = youtube.metadata({"title": "T", "uploader": "U", "timestamp": 0, "upload_date": "20200101", "duration": 9})
    assert meta == {
        "title": "T", "channel": "U", "channel_id": None, "channel_url": None,
        "published_at": "1970-01-01T00:00:00+00:00", "duration_seconds": 9, "description": None,
    }


BOARD = {
    "format_note": "storyboard", "width": 30, "height": 20, "rows": 3, "columns": 3, "fps": 0.5,
    "fragments": [{"url": "https://sb.test/M0.jpg", "duration": 18.0},
                  {"url": "https://sb.test/M1.jpg", "duration": 18.0}],
}


def test_storyboard_tile_locates_frames():
    assert youtube.storyboard_tile(BOARD, 0) == (0, 0, 0)
    assert youtube.storyboard_tile(BOARD, 2.5) == (0, 30, 0)     # tile 1
    assert youtube.storyboard_tile(BOARD, 8.5) == (0, 30, 20)    # tile 4
    assert youtube.storyboard_tile(BOARD, 19) == (1, 0, 0)       # second sheet
    assert youtube.storyboard_tile(BOARD, 9999) == (1, 60, 40)   # clamped to last tile


def test_storyboard_tile_on_the_partial_last_sheet():
    # yt-dlp gives the last sheet only the remainder (4s here: two tiles); its tiles are still 2s apart
    board = {**BOARD, "fragments": [*BOARD["fragments"], {"url": "https://sb.test/M2.jpg", "duration": 4.0}]}
    assert youtube.storyboard_tile(board, 36.5) == (2, 0, 0)
    assert youtube.storyboard_tile(board, 38.5) == (2, 30, 0)  # not spread over the whole 3x3 sheet
    assert youtube.storyboard_tile(board, 9999) == (2, 30, 0)  # clamped to the last tile it has
    without_fps = {k: v for k, v in board.items() if k != "fps"}
    assert youtube.storyboard_tile(without_fps, 38.5) == (2, 30, 0)  # the interval of a full sheet


def red_sheet(tmp_path) -> Path:
    sheet = tmp_path / "sheet.jpg"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=90x60",
                    "-frames:v", "1", str(sheet)], check=True)
    return sheet


def test_storyboard_frames_fetch_fresh_urls_once_when_youtube_refuses_old_ones(tmp_path):
    sheet = red_sheet(tmp_path)
    fetched, refreshes = [], []

    def handler(request):
        fetched.append(request.url.params["sigh"])
        if request.url.params["sigh"] == "old":
            return httpx.Response(403)
        return httpx.Response(200, content=sheet.read_bytes())

    def signed(sigh):
        return {**BOARD, "fragments": [{**f, "url": f"{f['url']}?sigh={sigh}"} for f in BOARD["fragments"]]}

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frames = youtube.StoryboardFrames({"formats": [signed("old")]}, tmp_path, client,
                                          refresh=lambda: refreshes.append(1) or {"formats": [signed("new")]})
        assert frames.frame(2.5, tmp_path / "a.jpg") and frames.frame(19, tmp_path / "b.jpg")
        assert fetched == ["old", "new", "new"] and refreshes == [1]
        stale = youtube.StoryboardFrames({"formats": [signed("old")]}, tmp_path, client,
                                         refresh=lambda: {"formats": [signed("old")]})
        assert stale.frame(2.5, tmp_path / "c.jpg") is False  # refreshed once, then given up
        assert stale.frame(19, tmp_path / "d.jpg") is False and fetched[-2:] == ["old", "old"]

        def gone():
            raise youtube.YouTubeError("Video unavailable")

        broken = youtube.StoryboardFrames({"formats": [signed("old")]}, tmp_path, client, refresh=gone)
        before = len(fetched)
        assert broken.frame(2.5, tmp_path / "e.jpg") is False  # no fresh info: no more thumbnails
        assert broken.frame(19, tmp_path / "f.jpg") is False and len(fetched) == before + 1


def test_storyboard_frames_crop_a_tile(tmp_path):
    sheet = tmp_path / "sheet.jpg"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=90x60",
                    "-frames:v", "1", str(sheet)], check=True)
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        return httpx.Response(200, content=sheet.read_bytes())

    info = {"formats": [{"format_note": "storyboard", "width": 10, "fragments": [{"url": "x"}], "rows": 1, "columns": 1}, BOARD]}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frames = youtube.StoryboardFrames(info, tmp_path, client)
        assert frames.board is BOARD  # widest storyboard wins
        assert frames.frame(2.5, tmp_path / "a.jpg")
        assert frames.frame(4.5, tmp_path / "b.jpg")
    assert fetched == ["https://sb.test/M0.jpg"]  # sheet fetched once
    assert (tmp_path / "a.jpg").stat().st_size > 0


def test_storyboard_frames_without_board(tmp_path):
    with httpx.Client() as client:
        assert youtube.StoryboardFrames({"formats": []}, tmp_path, client).frame(1, tmp_path / "x.jpg") is False


def track(lang, tlang=None):
    url = f"https://yt.test/timedtext?lang={lang}" + (f"&tlang={tlang}" if tlang else "")
    return [{"ext": "vtt", "url": url + "&fmt=vtt"}, {"ext": "json3", "url": url + "&fmt=json3"}]


def test_pick_caption_track_prefers_creator_captions_in_spoken_language():
    info = {"language": "en", "subtitles": {"de-DE": track("de"), "en-US": track("en"), "live_chat": track("x")},
            "automatic_captions": {"en-orig": track("en")}}
    picked = youtube.pick_caption_track(info)
    assert (picked.source, picked.language) == ("youtube", "en-US")


def test_pick_caption_track_uses_original_asr_track_for_dubbed_videos():
    info = {"language": "en-US", "subtitles": {},
            "automatic_captions": {"bn-orig": track("bn"), "en-orig": track("en"), "fr": track("en", "fr")}}
    picked = youtube.pick_caption_track(info)
    assert (picked.source, picked.language) == ("youtube_auto", "en")
    assert "lang=en" in picked.url and "tlang" not in picked.url


def test_pick_caption_track_modes_and_misses():
    info = {"language": "en", "automatic_captions": {"en-orig": track("en")}}
    assert youtube.pick_caption_track(info, "manual") is None
    assert youtube.pick_caption_track(info, "off") is None
    # unknown language: only safe when there's a single original track
    assert youtube.pick_caption_track({"automatic_captions": {"en-orig": track("en")}}).language == "en"
    assert youtube.pick_caption_track({"automatic_captions": {"en-orig": track("en"), "es-orig": track("es")}}) is None
    # translations only, or no json3 (very old videos): fall back to Parakeet
    assert youtube.pick_caption_track({"language": "en", "automatic_captions": {"en": track("de", "en")}}) is None
    assert youtube.pick_caption_track({"language": "en", "subtitles": {"en": [{"ext": "vtt", "url": "u"}]}}) is None


def test_info_cache_reuses_recent_lookups(monkeypatch):
    calls = []
    monkeypatch.setattr(youtube, "fetch_info", lambda v: calls.append(v) or {"id": v})
    cache = youtube.InfoCache(ttl_seconds=60, size=2)
    cache.get("a"); cache.get("a"); cache.get("b"); cache.get("c"); cache.get("a")
    assert calls == ["a", "b", "c", "a"]  # "a" was evicted once the cache held b and c
    expired = youtube.InfoCache(ttl_seconds=0)
    expired.get("x"); expired.get("x")
    assert calls[-2:] == ["x", "x"]


def test_to_mp3_converts_and_removes_the_download(tmp_path):
    source = tmp_path / "abc.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libopus",
                    str(source)], check=True)
    out = youtube.to_mp3(source)
    assert out == tmp_path / "abc.mp3" and out.stat().st_size > 0 and not source.exists()
    assert youtube.to_mp3(out) == out  # already MP3


def test_to_mp3_stops_when_check_raises(tmp_path):
    source = tmp_path / "stuck.webm"
    os.mkfifo(source)  # nothing ever writes to it, so ffmpeg waits forever
    calls = []

    def check():
        calls.append(time.monotonic())
        raise RuntimeError("cancelled")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cancelled"):
        youtube.to_mp3(source, check)
    assert len(calls) == 1 and time.monotonic() - started < 3
    assert not (tmp_path / "stuck.mp3").exists()


def test_to_mp3_reports_ffmpeg_failures(tmp_path):
    source = tmp_path / "junk.webm"
    source.write_bytes(b"not audio")
    with pytest.raises(youtube.YouTubeError, match="Could not convert the audio"):
        youtube.to_mp3(source)
    assert not (tmp_path / "junk.mp3").exists()
