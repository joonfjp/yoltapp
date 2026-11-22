"""Clearing the catalogue: every episode and all made from it goes; subscriptions and settings stay."""

import time

import psycopg
from psycopg.rows import dict_row

from tests.support.fakes import CHANNEL_ID, submit, wait_for
from tests.test_subscriptions import subscribe, videos, wait_checked
from yolt import youtube
from yolt.db import Database

B = "/api/v1"
KEPT_TABLES = {"settings", "schema_migrations", "podcasts", "subscriptions", "channel_videos", "embedding_status"}


def row_counts(database_url) -> dict[str, int]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        tables = [r["tablename"] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
        return {t: conn.execute(f'SELECT count(*) AS n FROM "{t}"').fetchone()["n"] for t in tables}


def test_clear_deletes_every_clip_and_thumbnail_but_keeps_subscriptions_and_settings(client, fake, tmp_path,
                                                                                  database_url):
    fake.info_extra = {"channel_id": CHANNEL_ID}
    sub = wait_checked(client, subscribe(client, "@pod").json()["id"])
    run_id = submit(client, youtube.watch_url("upl00000003"))
    clips = wait_for(client, run_id, "ready")["clips"]
    fake.info_extra = {"channel_id": "UC" + "q" * 22}  # a channel nobody follows
    wait_for(client, submit(client, youtube.watch_url("aaaaaaaaaaa")), "ready")
    compilation = client.post(f"{B}/compilations", json={"title": "Best of", "items": [{"clip_id": clips[0]["id"]}]})
    assert compilation.status_code == 201, compilation.text
    assert client.put(f"{B}/settings", json={"max_clips": "7"}).status_code == 200
    thumbs = tmp_path / "thumbs"
    (thumbs / "some-clip.jpg").write_bytes(b"jpeg")
    (tmp_path / "work" / "audio").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "audio" / "upl00000003.mp3").write_bytes(b"ID3")

    refused = client.post(f"{B}/catalogue/clear", json={})
    assert refused.status_code == 422 and refused.json()["errors"][0]["pointer"] == "/confirm"
    assert client.post(f"{B}/catalogue/clear", json={"confirm": "yes"}).status_code == 422
    assert client.get(f"{B}/clips").json()["data"]  # nothing went

    cleared = client.post(f"{B}/catalogue/clear", json={"confirm": "clear"})
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["episodes"] == 2 and body["runs"] == 2 and body["clips"] >= len(clips) >= 1
    assert (body["compilations"], body["thumbnails"]) == (1, 1)

    assert list(thumbs.glob("*")) == [] and list((tmp_path / "work" / "audio").glob("*")) == []
    for path in ("/clips", "/episodes", "/runs", "/compilations", "/speakers"):
        assert client.get(f"{B}{path}").json()["data"] == [], path
    counts = row_counts(database_url)
    assert {t for t, n in counts.items() if n} <= KEPT_TABLES  # a new table is cleared or listed as kept
    assert counts["podcasts"] == 1  # the followed channel; the other one went with its episode

    assert [s["id"] for s in client.get(f"{B}/subscriptions").json()["data"]] == [sub["id"]]
    found = videos(client)
    assert {v["video_id"] for v in found} == {"upl00000001", "upl00000003"}
    assert all(v["run"] is None and v["episode_id"] is None for v in found)
    assert client.get(f"{B}/settings").json()["values"]["max_clips"] == "7"
    assert client.get(f"{B}/queue").json()["totals"]["episodes"] == 0

    fake.info_extra = {"channel_id": CHANNEL_ID}
    again = submit(client, youtube.watch_url("upl00000003"))  # a fresh start works as before
    assert wait_for(client, again, "ready")["clips"]


def test_a_busy_catalogue_answers_409(client, database_url, monkeypatch):
    monkeypatch.setattr(Database, "CLEAR_LOCK_TIMEOUT", "50ms")
    with psycopg.connect(database_url) as conn:
        conn.execute("LOCK TABLE clips IN SHARE MODE")  # e.g. a long write holding it
        response = client.post(f"{B}/catalogue/clear", json={"confirm": "clear"})
    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("/busy")


def test_a_run_in_progress_stops_and_the_worker_moves_on(client, fake):
    fake.hold_downloads.add("bbbbbbbbbbb")  # no captions: it downloads, and the download hangs
    held = submit(client, youtube.watch_url("bbbbbbbbbbb"))
    wait_for(client, held, "processing")
    deadline = time.time() + 10
    while "bbbbbbbbbbb" not in fake.downloads and time.time() < deadline:
        time.sleep(0.02)
    assert client.post(f"{B}/catalogue/clear", json={"confirm": "clear"}).json()["runs"] == 1
    assert client.get(f"{B}/runs/{held}").status_code == 404
    fake.hold_downloads.clear()
    after = submit(client, youtube.watch_url("ccccccccccc"))
    assert wait_for(client, after, "ready")["clips"]
    assert [r["id"] for r in client.get(f"{B}/runs").json()["data"]] == [after]
