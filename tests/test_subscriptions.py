"""Channel subscriptions: following uploads, finished livestreams or both, and the videos checks find."""

import time
from datetime import datetime, timezone

import pytest

from tests.support.fakes import CHANNEL_ID, entry, submit, wait_for
from yolt import subscriptions, youtube
from yolt.db import Database
from yolt.repo import subscriptions as repo

B = "/api/v1"


def subscribe(client, url="@pod", **body):
    response = client.post(f"{B}/subscriptions", json={"url": url, **body})
    assert response.status_code in (200, 201), response.text
    return response


def wait_checked(client, subscription_id, timeout=10) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sub = client.get(f"{B}/subscriptions/{subscription_id}").json()
        if not sub["check_pending"]:
            return sub
        time.sleep(0.05)
    raise AssertionError(f"check never finished: {sub}")


def videos(client, query=""):
    response = client.get(f"{B}/subscriptions/videos{query}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- channel links -----------------------------------------------------------------

@pytest.mark.parametrize("text, path", [
    ("@pod", "/@pod"),
    ("  @Some.Pod-1  ", "/@Some.Pod-1"),
    ("https://www.youtube.com/@pod", "/@pod"),
    ("youtube.com/@pod/streams?view=2", "/@pod"),
    ("https://m.youtube.com/channel/UCSHZKyawb77ixDdsGog4iWA/videos", "/channel/UCSHZKyawb77ixDdsGog4iWA"),
    ("UCSHZKyawb77ixDdsGog4iWA", "/channel/UCSHZKyawb77ixDdsGog4iWA"),
    ("https://www.youtube.com/c/LinusTechTips", "/c/LinusTechTips"),
    ("https://www.youtube.com/user/LinusTechTips/featured", "/user/LinusTechTips"),
    ("https://www.youtube.com/@%E6%97%A5%E6%9C%AC%E8%AA%9E%E3%81%A7%E3%81%99", "/@日本語です"),
])
def test_channel_links_become_canonical_paths(text, path):
    assert youtube.channel_path(text) == path


@pytest.mark.parametrize("text", [
    "", "@a", "@pod/../x", "https://example.com/@pod", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "ftp://www.youtube.com/@pod", "https://www.youtube.com/channel/UCtooshort", "javascript:alert(1)",
    "https://www.youtube.com/c/", "https://www.youtube.com/@pod name",
])
def test_anything_else_is_refused(text):
    with pytest.raises(youtube.YouTubeError):
        youtube.channel_path(text)


def test_the_page_yt_dlp_gets_is_rebuilt_and_quoted():
    assert youtube.channel_page("/@日本語です", "videos") == \
        "https://www.youtube.com/@%E6%97%A5%E6%9C%AC%E8%AA%9E%E3%81%A7%E3%81%99/videos"
    assert youtube.channel_page("/channel/UCSHZKyawb77ixDdsGog4iWA") == \
        "https://www.youtube.com/channel/UCSHZKyawb77ixDdsGog4iWA"


def test_yt_dlp_channel_errors_lose_their_prefix():
    error = Exception("ERROR: [youtube:tab] @lexfridman: This channel does not have a streams tab")
    assert youtube._clean_ytdlp_error(error) == "This channel does not have a streams tab"
    error = Exception("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable")
    assert youtube._clean_ytdlp_error(error) == "Video unavailable"


# --- what a check keeps -------------------------------------------------------------

def test_a_check_keeps_the_newest_three_long_videos_of_the_newest_five():
    info = {"entries": [entry("aaaaaaaaaa1", 3600), entry("aaaaaaaaaa2", 60), entry("aaaaaaaaaa3", 1800),
                        entry("aaaaaaaaaa4", 4000), entry("aaaaaaaaaa5", 100), entry("aaaaaaaaaa6", 5000)]}
    kept = subscriptions.tab_videos(info, "upload", min_seconds=1800)
    assert [v["video_id"] for v in kept] == ["aaaaaaaaaa1", "aaaaaaaaaa3", "aaaaaaaaaa4"]
    info["entries"][0]["duration"] = 10  # now only two of the newest five are long enough; the 6th isn't looked at
    assert [v["video_id"] for v in subscriptions.tab_videos(info, "upload", min_seconds=1800)] == \
        ["aaaaaaaaaa3", "aaaaaaaaaa4"]
    first = kept[0]
    assert first["duration_ms"] == 3_600_000 and first["title"] == "Video aaaaaaaaaa1"
    assert isinstance(first["published_at"], datetime) and first["published_at"].tzinfo is timezone.utc


def test_live_upcoming_members_only_and_odd_entries_are_skipped():
    info = {"entries": [
        entry("bbbbbbbbbb1", 3600, live_status="is_live", duration=None),
        entry("bbbbbbbbbb2", 3600, live_status="is_upcoming", duration=None),
        entry("bbbbbbbbbb3", 3600, live_status="post_live"),
        entry("bbbbbbbbbb4", 3600, availability="subscriber_only"),
        entry("bbbbbbbbbb5", 3600, live_status="was_live", timestamp=None),  # a premiere, say
    ]}
    kept = subscriptions.tab_videos(info, "upload", min_seconds=60)
    assert [v["video_id"] for v in kept] == ["bbbbbbbbbb5"] and kept[0]["published_at"] is None
    odd = {"entries": [entry("not-a-video-id", 3600), {"id": "bbbbbbbbbb6", "duration": 3600}]}
    assert [(v["video_id"], v["title"]) for v in subscriptions.tab_videos(odd, "upload", min_seconds=60)] == \
        [("bbbbbbbbbb6", "YouTube video bbbbbbbbbb6")]


def test_the_live_tab_keeps_only_finished_streams_and_looks_further_back():
    info = {"entries": [entry(f"ccccccccc{n:02d}", 600) for n in range(20)]
            + [entry("ccccccccc20", 3600, live_status="was_live"), entry("ccccccccc21", 3600, live_status=None)]}
    assert [v["video_id"] for v in subscriptions.tab_videos(info, "stream", min_seconds=1800)] == ["ccccccccc20"]


def test_the_schedule_comes_from_settings():
    assert subscriptions.schedule({"subscription_check_hours": "6", "subscription_min_minutes": "30"}) == (6, 1800)
    assert subscriptions.schedule({"subscription_check_hours": "", "subscription_min_minutes": "x"}) == (0, 0)


# --- the API -------------------------------------------------------------------------

def test_subscribe_to_uploads_check_and_list_what_was_found(client, fake):
    response = subscribe(client, "https://www.youtube.com/@pod")
    assert response.status_code == 201
    sub = response.json()
    assert response.headers["location"] == f"{B}/subscriptions/{sub['id']}"
    assert (sub["channel_id"], sub["name"], sub["handle"]) == (CHANNEL_ID, "Some Pod", "@pod")
    assert (sub["uploads"], sub["streams"]) == (True, False)

    sub = wait_checked(client, sub["id"])
    assert sub["checked_at"] and sub["check_error"] is None
    assert (sub["video_count"], sub["unclipped_count"]) == (2, 2)
    assert sub["next_check_at"] > sub["checked_at"]  # every 6 hours by default
    # The lookup, then the Videos tab only: uploads are all it follows.
    assert [(tab, limit) for _, tab, limit in fake.channel_calls] == [(None, 1), ("videos", 5)]
    assert fake.channel_calls[1][0] == f"/channel/{CHANNEL_ID}"

    found = videos(client)
    assert [v["video_id"] for v in found] == ["upl00000001", "upl00000003"]  # the 10-minute one is too short
    first = found[0]
    assert first["kind"] == "upload" and first["duration_ms"] == 3_600_000
    assert first["url"] == youtube.watch_url("upl00000001")
    assert first["thumbnail_url"] == "https://i.ytimg.com/vi/upl00000001/mqdefault.jpg"  # small, 16:9
    assert first["channel"] == {"podcast_id": sub["podcast_id"], "name": "Some Pod",
                                "url": f"https://www.youtube.com/channel/{CHANNEL_ID}", "subscription_id": sub["id"]}
    assert (first["episode_id"], first["run"]) == (None, None)

    listed = client.get(f"{B}/subscriptions").json()
    assert [s["id"] for s in listed["data"]] == [sub["id"]] and listed["next_cursor"] is None


def test_subscribing_again_sets_what_it_follows_and_checks_the_new_kind(client, fake):
    sub = subscribe(client, "@pod").json()
    wait_checked(client, sub["id"])
    again = subscribe(client, f"https://www.youtube.com/channel/{CHANNEL_ID}", streams=True)
    assert again.status_code == 200
    assert again.json()["id"] == sub["id"] and again.json()["streams"] is True
    wait_checked(client, sub["id"])
    streams = videos(client, "?kind=stream")
    assert [v["video_id"] for v in streams] == ["str00000002"]  # the upcoming one isn't kept
    assert streams[0]["kind"] == "stream"
    assert {v["video_id"] for v in videos(client, "?kind=upload")} == {"upl00000001", "upl00000003"}


def test_livestreams_only(client, fake):
    sub = subscribe(client, "@pod", uploads=False, streams=True).json()
    wait_checked(client, sub["id"])
    assert [tab for _, tab, _ in fake.channel_calls] == [None, "streams"]
    assert [v["video_id"] for v in videos(client)] == ["str00000002"]


def test_a_channel_that_never_streamed_is_not_a_failed_check(client, fake):
    del fake.tabs[(CHANNEL_ID, "streams")]
    sub = wait_checked(client, subscribe(client, "@pod", streams=True).json()["id"])
    assert sub["check_error"] is None and sub["video_count"] == 2


def test_subscribe_validation(client, fake):
    response = client.post(f"{B}/subscriptions", json={"url": "https://example.com/@pod", "uploads": False})
    assert response.status_code == 422
    assert {(e["code"], e["pointer"]) for e in response.json()["errors"]} == {
        ("not_youtube", "/url"), ("nothing_followed", "/uploads")}
    missing = client.post(f"{B}/subscriptions", json={"url": "@nobody-here"})
    assert missing.status_code == 422 and missing.json()["errors"][0]["code"] == "channel_unavailable"
    fake.channel_error = youtube.YouTubeError("Sign in to confirm you're not a bot")
    blocked = client.post(f"{B}/subscriptions", json={"url": "@pod"})
    assert blocked.status_code == 422 and "not a bot" in blocked.json()["errors"][0]["message"]
    extra = client.post(f"{B}/subscriptions", json={"url": "@pod", "videos": True})
    assert extra.status_code == 422 and extra.json()["errors"][0]["code"] == "unknown_field"
    assert client.get(f"{B}/subscriptions").json()["data"] == []


def test_a_video_link_subscribes_to_the_channel_its_on(client, fake):
    fake.info_extra = {"channel_id": CHANNEL_ID}
    response = subscribe(client, "https://www.youtube.com/watch?v=vid00000001&t=30s")
    assert response.status_code == 201
    assert (response.json()["channel_id"], response.json()["handle"]) == (CHANNEL_ID, "@pod")
    assert fake.channel_calls[0][:2] == (f"/channel/{CHANNEL_ID}", None)  # looked up by the id the video gave
    # The same video as a short link or a bare id is the same channel, and its details aren't fetched again.
    for url in ("https://youtu.be/vid00000001", "vid00000001"):
        again = subscribe(client, url)
        assert again.status_code == 200 and again.json()["id"] == response.json()["id"]
    assert fake.info_calls == 1


def test_lookup_says_whether_the_videos_channel_is_followed(client, fake):
    nameless = client.get(f"{B}/lookup", params={"url": "vid00000001"}).json()
    assert (nameless["channel_id"], nameless["subscription"]) == (None, None)  # the fake's videos name no channel id
    fake.info_extra = {"channel_id": CHANNEL_ID}
    before = client.get(f"{B}/lookup", params={"url": "vid00000002"}).json()
    assert (before["channel_id"], before["subscription"]) == (CHANNEL_ID, None)
    sub = subscribe(client, before["url"]).json()  # what the submit modal's Subscribe sends
    after = client.get(f"{B}/lookup", params={"url": "vid00000002"}).json()
    assert after["subscription"] == {"id": sub["id"], "uploads": True, "streams": False}


def test_a_video_link_needs_a_video_youtube_knows_on_a_channel(client, fake):
    fake.info_error = youtube.YouTubeError("Video unavailable")
    gone = client.post(f"{B}/subscriptions", json={"url": "https://youtu.be/vid00000001"})
    assert gone.status_code == 422 and gone.json()["errors"][0]["code"] == "video_unavailable"
    fake.info_error = None  # the fake's videos name no channel id
    nameless = client.post(f"{B}/subscriptions", json={"url": "https://youtu.be/vid00000002"})
    assert nameless.status_code == 422 and nameless.json()["errors"][0]["code"] == "channel_unavailable"
    assert fake.channel_calls == [] and client.get(f"{B}/subscriptions").json()["data"] == []


def test_change_what_a_subscription_follows(client, fake):
    sub = wait_checked(client, subscribe(client, "@pod").json()["id"])
    neither = client.patch(f"{B}/subscriptions/{sub['id']}", json={"uploads": False})
    assert neither.status_code == 422 and neither.json()["errors"][0]["code"] == "nothing_followed"

    unchanged = client.patch(f"{B}/subscriptions/{sub['id']}", json={})
    assert unchanged.status_code == 200 and unchanged.json()["check_pending"] is False

    switched = client.patch(f"{B}/subscriptions/{sub['id']}", json={"uploads": False, "streams": True})
    assert switched.status_code == 200
    assert (switched.json()["uploads"], switched.json()["streams"]) == (False, True)
    wait_checked(client, sub["id"])
    assert fake.channel_calls[-1][1] == "streams"
    assert len(videos(client)) == 3  # the uploads found earlier stay

    assert client.patch(f"{B}/subscriptions/999", json={"streams": True}).status_code == 404
    assert client.get(f"{B}/subscriptions/999").status_code == 404


def test_check_now_and_failed_checks(client, fake):
    sub = wait_checked(client, subscribe(client, "@pod").json()["id"])
    calls = len(fake.channel_calls)
    fake.channel_error = youtube.YouTubeError("HTTP Error 429: Too Many Requests")
    response = client.post(f"{B}/subscriptions/check")
    assert response.status_code == 202 and response.json() == {"pending": 1}
    failed = wait_checked(client, sub["id"])
    assert failed["check_error"] == "HTTP Error 429: Too Many Requests"
    assert failed["next_check_at"] > failed["checked_at"]  # retried in a minute
    assert len(fake.channel_calls) == calls + 1

    fake.channel_error = None
    client.post(f"{B}/subscriptions/check")
    assert wait_checked(client, sub["id"])["check_error"] is None


def test_stats_count_the_latest_rounds_new_videos_and_the_last_weeks_clips(client, fake, database_url):
    assert client.get(f"{B}/stats").json() == {"subscriptions": {"last_checked_at": None, "new_videos": 0},
                                               "clipped": {"days": 7, "clips": 0, "episodes": 0}}
    sub = wait_checked(client, subscribe(client).json()["id"])
    run = wait_for(client, submit(client), "ready")
    stats = client.get(f"{B}/stats").json()
    assert stats["subscriptions"] == {"last_checked_at": sub["checked_at"], "new_videos": 2}
    assert stats["clipped"] == {"days": 7, "clips": len(run["clips"]), "episodes": 1} and run["clips"]

    # A day later another round finds the same videos, so nothing new; the run, 8 days old now, is out.
    db = Database(database_url)
    with db.connect() as conn:
        conn.execute("UPDATE subscriptions SET checked_at = checked_at - interval '1 day'")
        conn.execute("UPDATE channel_videos SET discovered_at = discovered_at - interval '1 day'")
        conn.execute("UPDATE runs SET finished_at = finished_at - interval '8 days'")
    db.close()
    client.post(f"{B}/subscriptions/check")
    sub = wait_checked(client, sub["id"])
    stats = client.get(f"{B}/stats").json()
    assert stats["subscriptions"] == {"last_checked_at": sub["checked_at"], "new_videos": 0}
    assert stats["clipped"] == {"days": 7, "clips": 0, "episodes": 0}


def test_check_one_subscription_now(client, fake):
    first = wait_checked(client, subscribe(client, "@pod").json()["id"])
    fake.channel_error = youtube.YouTubeError("HTTP Error 429: Too Many Requests")
    client.post(f"{B}/subscriptions/check")
    assert wait_checked(client, first["id"])["check_error"]

    fake.channel_error = None
    calls = len(fake.channel_calls)
    response = client.post(f"{B}/subscriptions/{first['id']}/check")
    assert response.status_code == 202 and response.json()["id"] == first["id"]
    assert response.json()["check_pending"] is True
    retried = wait_checked(client, first["id"])
    assert retried["check_error"] is None and retried["checked_at"] > first["checked_at"]
    assert len(fake.channel_calls) == calls + 1  # its uploads tab only

    assert client.post(f"{B}/subscriptions/999/check").status_code == 404


def test_no_scheduled_checks_when_the_interval_is_zero(client, fake):
    assert client.put(f"{B}/settings", json={"subscription_check_hours": "0"}).status_code == 200
    sub = wait_checked(client, subscribe(client, "@pod").json()["id"])  # subscribing still checks once
    assert sub["next_check_at"] is None and sub["checked_at"]


def test_the_shortest_video_kept_is_a_setting(client, fake):
    assert client.put(f"{B}/settings", json={"subscription_min_minutes": "50"}).status_code == 200
    wait_checked(client, subscribe(client, "@pod").json()["id"])
    assert [v["video_id"] for v in videos(client)] == ["upl00000001"]


def test_subscription_settings_validation(client):
    view = client.get(f"{B}/settings").json()
    assert (view["defaults"]["subscription_check_hours"], view["defaults"]["subscription_min_minutes"]) == ("24", "30")
    bad = client.put(f"{B}/settings", json={"subscription_check_hours": "200", "subscription_min_minutes": "-5"})
    assert bad.status_code == 422
    assert {e["pointer"] for e in bad.json()["errors"]} == {"/subscription_check_hours", "/subscription_min_minutes"}
    reset = client.put(f"{B}/settings", json={"subscription_check_hours": "12"})
    assert reset.json()["values"]["subscription_check_hours"] == "12"


def test_found_videos_know_when_they_have_been_clipped(client, fake):
    sub = wait_checked(client, subscribe(client, "@pod").json()["id"])
    fake.info_extra = {"channel_id": CHANNEL_ID}
    run_id = submit(client, youtube.watch_url("upl00000003"))
    wait_for(client, run_id, "ready")

    clipped = videos(client, "?clipped=true")
    assert [v["video_id"] for v in clipped] == ["upl00000003"]
    assert clipped[0]["run"] == {"id": run_id, "status": "ready", "clip_count": 2}
    assert clipped[0]["episode_id"] is not None
    assert [v["video_id"] for v in videos(client, "?clipped=false")] == ["upl00000001"]
    assert client.get(f"{B}/subscriptions/{sub['id']}").json()["unclipped_count"] == 1
    # The episode belongs to the same channel the subscription follows.
    episode = client.get(f"{B}/episodes/{clipped[0]['episode_id']}").json()
    assert [e["id"] for e in client.get(f"{B}/episodes?podcast_id={sub['podcast_id']}").json()["data"]] == [episode["id"]]


def test_found_videos_page_and_filter(client, fake):
    fake.channels.append({"channel_id": "UC" + "q" * 22, "channel": "Other Show", "uploader_id": "@other",
                          "channel_url": "https://www.youtube.com/channel/UC" + "q" * 22})
    fake.tabs[("UC" + "q" * 22, "videos")] = [entry("oth00000001", 4000, days_ago=0)]
    first = wait_checked(client, subscribe(client, "@pod").json()["id"])
    other = wait_checked(client, subscribe(client, "@other").json()["id"])
    assert [s["name"] for s in client.get(f"{B}/subscriptions").json()["data"]] == ["Other Show", "Some Pod"]
    page = client.get(f"{B}/subscriptions?limit=1").json()
    assert page["next_cursor"] and client.get(f"{B}/subscriptions?limit=1&cursor={page['next_cursor']}").json()["data"]

    seen, cursor = [], None
    while True:
        query = "?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(f"{B}/subscriptions/videos{query}").json()
        seen += [v["video_id"] for v in page["data"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == ["oth00000001", "upl00000001", "upl00000003"]  # newest first
    assert [v["video_id"] for v in videos(client, f"?podcast_id={other['podcast_id']}")] == ["oth00000001"]
    assert len(videos(client, f"?podcast_id={other['podcast_id']}&podcast_id={first['podcast_id']}")) == 3
    first_page = client.get(f"{B}/subscriptions/videos?limit=1").json()
    stale = client.get(f"{B}/subscriptions/videos?kind=upload&cursor={first_page['next_cursor']}")
    assert stale.status_code == 400  # the cursor belongs to another filter
    assert client.get(f"{B}/subscriptions/videos?kind=live").status_code == 400
    assert client.get(f"{B}/subscriptions/videos?channel=x").status_code == 400


def test_unsubscribe_keeps_the_videos_found(client, fake):
    sub = wait_checked(client, subscribe(client, "@pod").json()["id"])
    response = client.delete(f"{B}/subscriptions/{sub['id']}")
    assert response.status_code == 204 and response.content == b""
    assert client.get(f"{B}/subscriptions").json()["data"] == []
    kept = videos(client)
    assert len(kept) == 2 and all(v["channel"]["subscription_id"] is None for v in kept)
    assert client.delete(f"{B}/subscriptions/{sub['id']}").status_code == 404
    resubscribed = subscribe(client, "@pod").json()
    assert resubscribed["video_count"] == 2  # the channel is the same podcast as before


def test_writes_from_other_sites_are_refused(client):
    response = client.post(f"{B}/subscriptions/check", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = client.delete(f"{B}/subscriptions/1", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_an_episode_knows_its_channel_subscription(client, fake):
    # yt-dlp names a video's channel by id, as the channel listing does, so both find the same channel row.
    fake.info_extra = {"channel_id": CHANNEL_ID, "channel_url": f"https://www.youtube.com/channel/{CHANNEL_ID}"}
    run = wait_for(client, submit(client), "ready")
    episode = client.get(f"{B}/episodes/{run['episode']['id']}").json()
    assert episode["subscription"] is None and episode["podcast_id"]

    sub = subscribe(client, episode["podcast_url"]).json()  # what the episode page's Subscribe sends
    assert sub["podcast_id"] == episode["podcast_id"]
    episode = client.get(f"{B}/episodes/{run['episode']['id']}").json()
    assert episode["subscription"] == {"id": sub["id"], "uploads": True, "streams": False}
    listed = client.get(f"{B}/episodes").json()["data"]
    assert listed[0]["subscription"]["id"] == sub["id"]


# --- the collector -------------------------------------------------------------------

@pytest.fixture
def collector(database_url, fake):
    """A collector that isn't running, and one subscription to uploads and streams, due now."""
    db = Database(database_url)
    with db.connect() as conn:
        sub_id, _ = repo.subscribe(conn, {"channel_id": CHANNEL_ID, "name": "Some Pod", "handle": "@pod",
                                          "url": f"https://www.youtube.com/channel/{CHANNEL_ID}"},
                                   uploads=True, streams=True)
    yield subscriptions.Collector(db), sub_id
    db.close()


def row(collector, sub_id) -> dict:
    with collector.db.connect() as conn:
        return conn.execute("SELECT checked_at, check_error, check_failures, check_requested_at FROM subscriptions "
                            "WHERE id = %s", (sub_id,)).fetchone()


def test_an_unexpected_error_is_a_failed_check_that_backs_off(collector, fake):
    collector, sub_id = collector
    fake.channel_error = TypeError("'NoneType' object is not subscriptable")  # a yt-dlp extractor bug
    with pytest.raises(TypeError):
        collector.check_next()
    failed = row(collector, sub_id)
    assert failed["checked_at"] and failed["check_failures"] == 1 and "TypeError" in failed["check_error"]
    assert failed["check_requested_at"] is None
    assert collector.check_next() is False  # not due again until the retry delay: it can't starve the rest


def test_a_check_cut_short_by_shutdown_is_not_recorded(collector, fake, monkeypatch):
    collector, sub_id = collector
    listed = youtube.fetch_channel

    def stop_midway(*args, **kwargs):
        collector._stopping.set()  # stop() lands while the Videos tab is being listed
        return listed(*args, **kwargs)

    monkeypatch.setattr(youtube, "fetch_channel", stop_midway)
    assert collector.check_next() is True
    assert [tab for _, tab, _ in fake.channel_calls] == ["videos"]  # the Live tab was never looked at
    partial = row(collector, sub_id)
    assert partial["checked_at"] is None and partial["check_requested_at"] is not None  # still due
    collector._stopping.clear()
    monkeypatch.setattr(youtube, "fetch_channel", listed)
    assert collector.check_next() is True
    assert row(collector, sub_id)["checked_at"] is not None


# --- auto-clip -------------------------------------------------------------------------

def test_auto_clip_queues_a_run_for_each_video_found_from_then_on(client, fake):
    fake.info_extra = {"channel_id": CHANNEL_ID}
    sub = wait_checked(client, subscribe(client, "@pod").json()["id"])
    assert (sub["autoclip"], sub["autoclip_since"]) == (False, None)

    on = client.patch(f"{B}/subscriptions/{sub['id']}", json={"autoclip": True})
    assert on.status_code == 200, on.text
    assert on.json()["autoclip"] is True and on.json()["autoclip_since"]
    assert on.json()["check_pending"] is False  # nothing new to look for
    assert client.get(f"{B}/runs").json()["data"] == []  # what was found already waits for Clip

    fake.tabs[(CHANNEL_ID, "videos")].insert(0, entry("upl00000004", 3000, days_ago=0))
    client.post(f"{B}/subscriptions/{sub['id']}/check")
    wait_checked(client, sub["id"])  # a finished check has queued its runs
    found = {v["video_id"]: v for v in videos(client)}
    assert found["upl00000001"]["run"] is None and found["upl00000003"]["run"] is None
    run = wait_for(client, found["upl00000004"]["run"]["id"], "ready")
    default = client.get(f"{B}/settings").json()["values"]["clip_instructions"].strip()
    assert run["instructions"] == default and run["options"] == {}
    on_channel = client.get(f"{B}/episodes?podcast_id={sub['podcast_id']}").json()["data"]
    assert [e["id"] for e in on_channel] == [run["episode"]["id"]]

    client.post(f"{B}/subscriptions/{sub['id']}/check")
    wait_checked(client, sub["id"])
    assert len(client.get(f"{B}/runs").json()["data"]) == 1  # once per video

    off = client.patch(f"{B}/subscriptions/{sub['id']}", json={"autoclip": False})
    assert (off.json()["autoclip"], off.json()["autoclip_since"]) == (False, None)
    fake.tabs[(CHANNEL_ID, "videos")].insert(0, entry("upl00000005", 3000, days_ago=0))
    client.post(f"{B}/subscriptions/{sub['id']}/check")
    wait_checked(client, sub["id"])
    assert {v["video_id"]: v["run"] for v in videos(client)}["upl00000005"] is None


def test_subscribe_with_auto_clip_and_subscribing_again_leaves_it(client, fake):
    sub = subscribe(client, "@pod", autoclip=True).json()
    assert sub["autoclip"] is True
    sub = wait_checked(client, sub["id"])  # its first check's videos are auto-clipped too
    assert {v["video_id"] for v in videos(client) if v["run"]} == {"upl00000001", "upl00000003"}
    again = subscribe(client, f"https://www.youtube.com/channel/{CHANNEL_ID}", streams=True).json()
    assert again["autoclip"] is True and again["autoclip_since"] == sub["autoclip_since"]
    assert subscribe(client, "@pod", autoclip=False).json()["autoclip"] is False


def test_auto_clip_skips_videos_with_any_run_and_wakes_the_worker(collector, fake):
    collector, sub_id = collector
    woken = []
    collector.queued = lambda: woken.append(True)
    with collector.db.connect() as conn:
        repo.update(conn, sub_id, uploads=None, streams=None, autoclip=True)

    def runs() -> dict:
        with collector.db.connect() as conn:
            return {r["video_id"]: r["status"] for r in conn.execute(
                "SELECT e.video_id, r.status FROM runs r JOIN episodes e ON e.id = r.episode_id")}

    assert collector.check_next() is True
    assert runs() == {"upl00000001": "queued", "upl00000003": "queued", "str00000002": "queued"}
    assert woken == [True]
    with collector.db.connect() as conn:
        conn.execute("UPDATE runs SET status = 'failed'")
        conn.execute("UPDATE channel_videos SET autoclipped_at = NULL")  # even unmarked, a run of any status counts
        repo.request_check(conn, sub_id)
    assert collector.check_next() is True
    assert set(runs().values()) == {"failed"} and woken == [True]  # failed runs aren't retried by themselves


# --- export and import ------------------------------------------------------------------

OTHER_ID = "UC" + "q" * 22


def test_export_then_import_brings_subscriptions_back(client, fake):
    fake.channels.append({"channel_id": OTHER_ID, "channel": "Other Pod", "uploader_id": "@other",
                          "channel_url": f"https://www.youtube.com/channel/{OTHER_ID}"})
    pod = wait_checked(client, subscribe(client, "@pod", streams=True).json()["id"])
    other = wait_checked(client, subscribe(client, "@other", autoclip=True).json()["id"])

    exported = client.get(f"{B}/subscriptions/export")
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-disposition"].startswith('attachment; filename="yolt-subscriptions-')
    file = exported.json()
    assert (file["format"], file["version"]) == ("yolt-subscriptions", 1) and file["exported_at"]
    assert file["subscriptions"] == [  # by name
        {"channel_id": OTHER_ID, "name": "Other Pod", "handle": "@other", "url": other["url"],
         "uploads": True, "streams": False, "autoclip": True},
        {"channel_id": CHANNEL_ID, "name": "Some Pod", "handle": "@pod", "url": pod["url"],
         "uploads": True, "streams": True, "autoclip": False},
    ]

    client.delete(f"{B}/subscriptions/{pod['id']}")
    client.delete(f"{B}/subscriptions/{other['id']}")
    calls = len(fake.channel_calls)
    imported = client.post(f"{B}/subscriptions/import", json=file)
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"created": 2, "already_subscribed": 0}
    subs = {s["channel_id"]: s for s in client.get(f"{B}/subscriptions").json()["data"]}
    assert {k: (s["uploads"], s["streams"], s["autoclip"], s["handle"]) for k, s in subs.items()} == {
        CHANNEL_ID: (True, True, False, "@pod"), OTHER_ID: (True, False, True, "@other")}
    assert all(tab for _, tab, _ in fake.channel_calls[calls:])  # only the checks asked YouTube anything
    assert not wait_checked(client, subs[CHANNEL_ID]["id"])["check_error"]  # each is checked as usual

    # Again: channels already followed are left as they are, even where the file differs.
    file["subscriptions"][1]["streams"] = False
    again = client.post(f"{B}/subscriptions/import", json=file)
    assert again.json() == {"created": 0, "already_subscribed": 2}
    assert client.get(f"{B}/subscriptions/{subs[CHANNEL_ID]['id']}").json()["streams"] is True


def test_import_by_hand_needs_only_channel_ids_and_keeps_known_names(client, fake):
    sub = wait_checked(client, subscribe(client, "@pod").json()["id"])
    client.delete(f"{B}/subscriptions/{sub['id']}")
    response = client.post(f"{B}/subscriptions/import", json={"subscriptions": [
        {"channel_id": CHANNEL_ID, "name": "An old name", "url": "javascript:alert(1)"},
        {"channel_id": OTHER_ID},
        {"channel_id": OTHER_ID, "streams": True},  # listed twice: the first counts
    ]})
    assert response.json() == {"created": 2, "already_subscribed": 0}
    subs = {s["channel_id"]: s for s in client.get(f"{B}/subscriptions").json()["data"]}
    assert subs[CHANNEL_ID]["name"] == "Some Pod"  # Yolt knew this channel already
    assert (subs[OTHER_ID]["name"], subs[OTHER_ID]["url"]) == (OTHER_ID, f"https://www.youtube.com/channel/{OTHER_ID}")
    assert (subs[OTHER_ID]["uploads"], subs[OTHER_ID]["streams"], subs[OTHER_ID]["autoclip"]) == (True, False, False)
    assert subs[CHANNEL_ID]["url"] == f"https://www.youtube.com/channel/{CHANNEL_ID}"  # never the file's link


def test_an_invalid_import_lists_every_problem_and_imports_nothing(client, fake):
    response = client.post(f"{B}/subscriptions/import", json={"format": "yolt-subscriptions", "subscriptions": [
        {"channel_id": CHANNEL_ID},
        {"channel_id": "UCtooshort"},
        {"channel_id": OTHER_ID, "uploads": False},
        {"channel_id": OTHER_ID, "handle": "@a b"},
    ]})
    assert response.status_code == 422, response.text
    assert [(e["code"], e["pointer"]) for e in response.json()["errors"]] == [
        ("invalid_value", "/subscriptions/1/channel_id"), ("nothing_followed", "/subscriptions/2/uploads"),
        ("invalid_value", "/subscriptions/3/handle")]
    for body in ({"format": "something-else", "subscriptions": []}, {"version": 2, "subscriptions": []},
                 {"subscriptions": [{"channel_id": CHANNEL_ID, "colour": "red"}]}, {}):
        assert client.post(f"{B}/subscriptions/import", json=body).status_code == 422
    assert client.get(f"{B}/subscriptions").json()["data"] == []
