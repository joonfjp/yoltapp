from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support.contract import ContractClient
from yolt import api, youtube


def test_download_uses_saved_range_and_cleans_up_on_success_and_failure(monkeypatch):
    database_open = False
    connection = Mock()
    connection.execute.return_value.fetchone.side_effect = [
        None,
        {"video_id": "aaaaaaaaaaa", "start_ms": 1320, "end_ms": 3110},
        {"video_id": "aaaaaaaaaaa", "start_ms": 1320, "end_ms": 3110},
    ]

    @contextmanager
    def connect():
        nonlocal database_open
        database_open = True
        try:
            yield connection
        finally:
            database_open = False

    paths: list[Path] = []

    def download(video_id: str, start_ms: int, end_ms: int, path: Path) -> None:
        assert not database_open
        assert (video_id, start_ms, end_ms) == ("aaaaaaaaaaa", 1320, 3110)
        paths.append(path)
        path.write_bytes(b"mock MP4")
        if len(paths) == 1:
            raise youtube.YouTubeError("Video unavailable.")

    monkeypatch.setattr(youtube, "download_mp4", download)
    app = FastAPI()
    api.install(app)
    app.state.db = SimpleNamespace(connect=connect)
    with TestClient(app) as client:
        contract = ContractClient(client)
        assert contract.post("/api/v1/clips/99/download").status_code == 404
        assert not paths

        failure = contract.post("/api/v1/clips/4/download")
        assert failure.status_code == 502
        assert failure.json()["detail"] == "Video unavailable."
        assert not paths[-1].parent.exists()

        response = contract.post("/api/v1/clips/4/download")
        assert response.status_code == 200
        assert response.content == b"mock MP4"
        assert response.headers["content-disposition"] == 'attachment; filename="clip-4.mp4"'
        assert response.headers["cache-control"] == "no-store"
        assert not paths[-1].parent.exists()
