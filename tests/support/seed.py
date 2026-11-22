"""Ready runs written straight to the database, for catalogue tests that don't need the worker."""

from yolt import ingest
from yolt.db import Database

TALKS = {
    "aaaaaaaaaaa": ("Minds and Machines", "Pod One", "2026-01-05",
                    "So what is consciousness really? I think consciousness is the hardest problem. "
                    "Machines may never feel anything at all. But they will certainly talk as if they do."),
    "bbbbbbbbbbb": ("Jobs After AI", "Pod Two", "2026-03-10",
                    "Automation will change every job we know. Some jobs vanish and new ones appear. "
                    "Nobody can say which jobs survive the next decade. That uncertainty is the real story."),
}


def seed(db: Database, video_id: str, instructions: str = "") -> dict:
    """A ready run: words half a second apart, a clip per sentence."""
    title, channel, published, text = TALKS[video_id]
    meta = {"title": title, "channel": channel, "channel_url": f"https://youtube.com/@{channel.replace(' ', '')}",
            "published_at": f"{published}T00:00:00+00:00", "duration_seconds": 600}
    run_id = db.submit(video_id, instructions, meta)
    episode_id = db.run(run_id)["episode_id"]
    words = ingest.word_rows([{"text": t, "start_seconds": i * 0.5, "end_seconds": i * 0.5 + 0.4}
                              for i, t in enumerate(text.split())])
    version = db.latest_transcript_id(episode_id) or db.save_transcript(
        episode_id, words, source="parakeet", model="m", language="en", duration_ms=600000)
    db.update_run(run_id, transcript_version_id=version)
    sentences, start = [], 0
    for i, w in enumerate(words):
        if w.word.endswith((".", "?")):
            sentences.append((start, i))
            start = i + 1
    result = {"clips": [{"rank": n + 1, "start_seconds": words[a].start_ms / 1000, "end_seconds": words[b].end_ms / 1000,
                         "best_headline": f"{title} #{n + 1}",
                         "summary": " ".join(w.word for w in words[a:b + 1]).lower(),
                         "best_quotes": [" ".join(w.word for w in words[a:a + 2])],
                         "scores": {"overall": round(0.9 - n * 0.2, 2)}}
                        for n, (a, b) in enumerate(sentences)]}
    db.finish_run(run_id, ingest.build_clips(result, words))
    return {"run_id": run_id, "episode_id": episode_id, "transcript_id": version}
