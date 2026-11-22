"""YouTube access: URL validation, metadata, audio download, storyboard frames."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


class YouTubeError(RuntimeError):
    pass


def parse_video_id(url: str) -> str:
    """Extract the 11-character video id, accepting only YouTube URLs.

    Only the id is ever handed to yt-dlp (as a canonical watch URL), so
    arbitrary user input never reaches the downloader.
    """
    raw = (url or "").strip()
    if VIDEO_ID_RE.match(raw):
        return raw
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise YouTubeError("Enter a YouTube link.")
    host = (parsed.hostname or "").lower()
    candidate = None
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in YOUTUBE_HOSTS:
        parts = [p for p in parsed.path.split("/") if p]
        if parts[:1] == ["watch"]:
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
            candidate = parts[1]
    if candidate and VIDEO_ID_RE.match(candidate):
        return candidate
    raise YouTubeError("That doesn't look like a YouTube video link.")


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def small_thumbnail_url(video_id: str) -> str:
    """320x180 and already 16:9 (hqdefault is 480x360 with black bars), at about two thirds the bytes:
    for cards and previews shown small. Every video has one."""
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def _ydl_options(**extra) -> dict:
    return {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True, **extra}


def fetch_info(video_id: str) -> dict:
    """Return yt-dlp's info dict for a video without downloading media."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
            return ydl.sanitize_info(ydl.extract_info(watch_url(video_id), download=False))
    except yt_dlp.utils.DownloadError as error:
        raise YouTubeError(_clean_ytdlp_error(error)) from error


class InfoCache:
    """Recently fetched video info, so checking a link and then submitting it
    only asks YouTube once. Entries expire well before YouTube's signed caption
    and storyboard URLs do."""

    def __init__(self, ttl_seconds: float = 600, size: int = 16):
        self.ttl = ttl_seconds
        self.size = size
        self._items: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, video_id: str) -> dict:
        now = time.monotonic()
        with self._lock:
            hit = self._items.get(video_id)
            if hit and now - hit[0] < self.ttl:
                self._items.move_to_end(video_id)
                return hit[1]
        info = fetch_info(video_id)
        with self._lock:
            self._items[video_id] = (now, info)
            self._items.move_to_end(video_id)
            while len(self._items) > self.size:
                self._items.popitem(last=False)
        return info


def metadata(info: dict) -> dict:
    """The subset of yt-dlp metadata stored on an episode."""
    published = None
    if info.get("timestamp") is not None:
        published = datetime.fromtimestamp(info["timestamp"], tz=timezone.utc).isoformat()
    elif info.get("upload_date"):
        d = info["upload_date"]
        published = f"{d[:4]}-{d[4:6]}-{d[6:8]}T00:00:00+00:00"
    return {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id") or info.get("uploader_id"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "published_at": published,
        "duration_seconds": info.get("duration"),
        "description": info.get("description"),
    }


CONVERT_CHECK_SECONDS = 0.5


def download_audio(
    video_id: str,
    workdir: Path,
    on_progress: Callable[[float], None] = lambda _: None,
    check: Callable[[], None] = lambda: None,
) -> Path:
    """Download the best audio stream and convert it to MP3 for Parakeet.

    `on_progress` gets the fraction downloaded, and `check` is called every half second of the
    conversion, which takes minutes for a long episode. If either raises, the download is abandoned
    or ffmpeg killed, and the exception propagates."""
    import yt_dlp

    workdir.mkdir(parents=True, exist_ok=True)

    def hook(status: dict) -> None:
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            if total:
                on_progress(min(1.0, status.get("downloaded_bytes", 0) / total))

    options = _ydl_options(format="bestaudio/best", outtmpl=str(workdir / "%(id)s.%(ext)s"), progress_hooks=[hook])
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(watch_url(video_id), download=True)
    except yt_dlp.utils.DownloadError as error:
        raise YouTubeError(_clean_ytdlp_error(error)) from error
    for download in info.get("requested_downloads") or []:
        path = Path(download.get("filepath") or "")
        if path.is_file():
            return to_mp3(path, check)
    raise YouTubeError("The audio download finished but produced no file.")


def to_mp3(source: Path, check: Callable[[], None] = lambda: None) -> Path:
    """Convert audio to MP3 as yt-dlp's FFmpegExtractAudio would, calling `check` every half second.

    If `check` raises, ffmpeg is killed, the partial MP3 removed, and the exception propagates."""
    if source.suffix.lower() == ".mp3":
        return source
    out = source.with_suffix(".mp3")
    try:
        process = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-vn",
             "-acodec", "libmp3lame", "-q:a", "5", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as error:
        raise YouTubeError("ffmpeg isn't installed; it converts the audio for Parakeet.") from error
    try:
        while True:
            try:
                _, errors = process.communicate(timeout=CONVERT_CHECK_SECONDS)
                break
            except subprocess.TimeoutExpired:
                check()
    except BaseException:
        process.kill()
        process.communicate()
        out.unlink(missing_ok=True)
        raise
    if process.returncode != 0:
        out.unlink(missing_ok=True)
        raise YouTubeError(f"Could not convert the audio to MP3: {errors.strip()[-300:] or process.returncode}")
    source.unlink(missing_ok=True)
    return out


def _clean_ytdlp_error(error: Exception) -> str:
    return re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[^\s:]+:\s*)?", "", str(error)).strip()


# --- Channels ------------------------------------------------------------------
# Subscriptions read a channel's tabs the way its page lists them: no API key, one request per tab.

CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^[\w.\-]{3,30}$")  # YouTube handles: 3-30 letters, digits, _ - .
LEGACY_NAME_RE = re.compile(r"^[\w.\-]{1,100}$")  # /c/<name> and /user/<name>
CHANNEL_TABS = {"upload": "videos", "stream": "streams"}


class NoSuchTab(YouTubeError):
    """The channel has no tab of that kind (e.g. it has never streamed)."""


def channel_path(text: str) -> str:
    """The canonical path of a channel from what someone typed: `/channel/UC…`, `/@handle`, `/c/name`
    or `/user/name`. Like parse_video_id, only this rebuilt path ever reaches yt-dlp."""
    raw = (text or "").strip()
    if CHANNEL_ID_RE.match(raw):
        return f"/channel/{raw}"
    if raw.startswith("@") and "/" not in raw:
        if HANDLE_RE.match(raw[1:]):
            return f"/{raw}"
    else:
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").lower()
        if parsed.scheme in {"http", "https"} and host in YOUTUBE_HOSTS:
            parts = [unquote(p) for p in parsed.path.split("/") if p]
            if parts and parts[0].startswith("@") and HANDLE_RE.match(parts[0][1:]):
                return f"/{parts[0]}"
            if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID_RE.match(parts[1]):
                return f"/channel/{parts[1]}"
            if len(parts) >= 2 and parts[0] in {"c", "user"} and LEGACY_NAME_RE.match(parts[1]):
                return f"/{parts[0]}/{parts[1]}"
    raise YouTubeError("Enter a YouTube @handle, channel URL, or UC… channel id.")


def channel_page(path: str, tab: str | None = None) -> str:
    return f"https://www.youtube.com{quote(path, safe='/@')}" + (f"/{tab}" if tab else "")


def fetch_channel(path: str, tab: str | None = None, *, limit: int = 1) -> dict:
    """yt-dlp's flat listing of a channel page (its home, or `tab`), newest first, at most `limit` entries.

    Entries carry the id, title, duration, `live_status` and YouTube's own approximate publish time
    ("3 days ago"). Raises NoSuchTab when the channel has no such tab, YouTubeError otherwise."""
    import yt_dlp

    options = _ydl_options(extract_flat="in_playlist", playlistend=limit,
                           extractor_args={"youtubetab": {"approximate_date": [""]}})
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.sanitize_info(ydl.extract_info(channel_page(path, tab), download=False))
    except yt_dlp.utils.DownloadError as error:
        message = _clean_ytdlp_error(error)
        if re.search(r"does not have an? \w+ tab", message):
            raise NoSuchTab(message) from error
        if "HTTP Error 404" in message:
            raise YouTubeError("No YouTube channel matches that.") from error
        raise YouTubeError(message) from error


def channel_details(info: dict) -> dict:
    """Who a channel listing (fetch_channel) belongs to."""
    channel_id = info.get("channel_id") or info.get("id")
    if not channel_id or not CHANNEL_ID_RE.match(channel_id):
        raise YouTubeError("YouTube didn't say which channel that is.")
    handle = info.get("uploader_id")
    return {
        "channel_id": channel_id,
        "name": info.get("channel") or info.get("uploader") or channel_id,
        "handle": handle if isinstance(handle, str) and handle.startswith("@") else None,
        "url": info.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}",
    }


# --- Captions ----------------------------------------------------------------
# When YouTube already has a transcript we use it instead of downloading audio
# and running Parakeet.


@dataclass(frozen=True)
class CaptionTrack:
    source: str  # "youtube" (uploaded by the creator) | "youtube_auto" (YouTube's ASR)
    language: str
    url: str


def _json3_url(formats: list[dict]) -> str | None:
    return next((f["url"] for f in formats if f.get("ext") == "json3" and f.get("url")), None)


def pick_caption_track(info: dict, mode: str = "any") -> CaptionTrack | None:
    """Choose the caption track in the video's spoken language, if any.

    Creator-uploaded captions win over auto-generated ones. Auto captions are
    taken only from the original-language ASR track (``<lang>-orig``), never a
    machine translation; dubbed videos carry one per audio language.
    """
    if mode == "off":
        return None
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    language = (info.get("language") or "").lower()
    base = language.split("-")[0]
    if not base:
        originals = [k[: -len("-orig")] for k in auto if k.endswith("-orig")]
        if len(originals) == 1:
            base = originals[0].split("-")[0]

    def same_language(key: str) -> bool:
        return key.lower().split("-")[0] == base

    if base:
        manual_keys = sorted(
            (k for k in manual if same_language(k)), key=lambda k: (k.lower() != language, len(k))
        )
    else:
        manual_keys = list(manual) if len(manual) == 1 else []
    for key in manual_keys:
        if url := _json3_url(manual[key]):
            return CaptionTrack("youtube", key, url)

    if mode == "manual" or not base:
        return None
    for key in (f"{base}-orig", base):
        formats = [f for f in auto.get(key) or [] if "tlang=" not in f.get("url", "")]
        if url := _json3_url(formats):
            return CaptionTrack("youtube_auto", key.removesuffix("-orig"), url)
    return None


def fetch_captions(track: CaptionTrack) -> dict:
    """Download a json3 caption track through yt-dlp's YouTube-aware client."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
            return json.loads(ydl.urlopen(track.url).read())
    except (yt_dlp.utils.DownloadError, yt_dlp.networking.exceptions.RequestError, ValueError) as error:
        raise YouTubeError(f"Could not load YouTube captions: {error}") from error


# --- Storyboard thumbnails -------------------------------------------------
# YouTube's seek-preview sprite sheets give a real frame near each clip's start
# without downloading any video.


def best_storyboard(info: dict) -> dict | None:
    boards = [
        f
        for f in info.get("formats") or []
        if f.get("format_note") == "storyboard"
        and f.get("fragments")
        and f.get("rows")
        and f.get("columns")
    ]
    return max(boards, key=lambda f: f.get("width") or 0) if boards else None


def storyboard_tile(board: dict, seconds: float) -> tuple[int, int, int] | None:
    """Return ``(fragment_index, x, y)`` of the tile showing ``seconds``.

    Tiles are one frame interval (1 / fps) apart on every sheet. yt-dlp gives the last sheet only the
    remainder's duration, so it's only partly filled: its tiles keep the same interval."""
    per_sheet = board["rows"] * board["columns"]
    fragments = board["fragments"]
    full = max(float(f.get("duration") or 0) for f in fragments)  # every sheet but the last is full
    interval = 1 / board["fps"] if board.get("fps") else (full / per_sheet if full else 2.0)
    elapsed = 0.0
    for index, fragment in enumerate(fragments):
        duration = float(fragment.get("duration") or per_sheet * interval)
        if seconds < elapsed + duration or index == len(fragments) - 1:
            tiles = min(per_sheet, max(1, round(duration / interval)))  # the ones this sheet has
            tile = min(int(max(0.0, seconds - elapsed) / interval), tiles - 1)
            row, column = divmod(tile, board["columns"])
            return index, column * board["width"], row * board["height"]
        elapsed += duration
    return None


class StoryboardFrames:
    """Crop clip thumbnails from storyboard sheets, fetching each sheet once.

    The sheet URLs are signed and expire, and a run can wait on Highlightr for hours: if YouTube
    refuses one (403), `refresh` is called, once, for the video's info again with fresh URLs."""

    def __init__(self, info: dict, workdir: Path, client: httpx.Client,
                 refresh: Callable[[], dict] | None = None):
        self.board = best_storyboard(info)
        self.workdir = workdir
        self.client = client
        self.refresh = refresh
        self.sheets: dict[int, Path] = {}

    def _renew(self) -> bool:
        """Swap in fresh sheet URLs; False if that was already tried or there's no storyboard now."""
        if self.refresh is None:
            return False
        refresh, self.refresh = self.refresh, None
        try:
            self.board = best_storyboard(refresh())
        except Exception:  # noqa: BLE001 - thumbnails are best effort: no fresh info means no more of them
            self.board = None
        self.sheets.clear()
        return self.board is not None

    def _sheet(self, index: int) -> Path:
        if index not in self.sheets:
            response = self.client.get(self.board["fragments"][index]["url"], timeout=20)
            response.raise_for_status()
            path = self.workdir / f"storyboard_{index}.jpg"
            path.write_bytes(response.content)
            self.sheets[index] = path
        return self.sheets[index]

    def frame(self, seconds: float, out: Path) -> bool:
        if not self.board:
            return False
        try:
            try:
                return self._crop(seconds, out)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 403 or not self._renew():
                    raise
                return self._crop(seconds, out)
        except (httpx.HTTPError, OSError, subprocess.SubprocessError):
            return False

    def _crop(self, seconds: float, out: Path) -> bool:
        located = storyboard_tile(self.board, seconds)
        if not located:
            return False
        index, x, y = located
        sheet = self._sheet(index)
        w, h = self.board["width"], self.board["height"]
        out.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(sheet), "-vf", f"crop={w}:{h}:{x}:{y}",
                "-frames:v", "1", "-q:v", "3", str(out),
            ],
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0 and out.is_file()
