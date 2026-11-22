"""Stand-in Parakeet, Highlightr and embedding server for UI work without a GPU or LLM.

    uv run python dev/fake_services.py     # parakeet :5092, highlightr :8000, embeddings :8098

Returns placeholder words, evenly spaced clips, and bag-of-words vectors (texts that share
words come out similar); for development only. To use the fake embeddings, set Settings →
Embeddings URL to http://127.0.0.1:8098/v1/embeddings. All three listen on 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import threading

import uvicorn
from fastapi import Body, FastAPI, File, Form, UploadFile

LINES = [
    "So the thing nobody tells you about starting out is how long it takes.",
    "I spent three years thinking I was behind, and it turns out everyone was.",
    "Here's what actually changed my mind about the whole thing.",
    "You have to be willing to look stupid in public for a while.",
    "And honestly that was the best decision I ever made.",
]

parakeet = FastAPI()
highlightr = FastAPI()
embeddings = FastAPI()


@parakeet.get("/health")
@highlightr.get("/health")
def health():
    return {"status": "ok"}


@parakeet.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form("")):
    data = await file.read()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", "-"],
        input=data, capture_output=True,
    )
    duration = float(probe.stdout or 240)
    text, words, t, i = [], [], 0.0, 0
    while t < duration - 1:
        line = LINES[i % len(LINES)]
        text.append(line)
        for token in re.findall(r"[\w']+", line):
            words.append({"word": token, "start": round(t, 2), "end": round(t + 0.32, 2)})
            t += 0.38
        t += 0.6
        i += 1
    return {"text": " ".join(text), "words": words, "duration": duration, "language": "en"}


JOBS: dict[str, dict] = {}


@highlightr.post("/v2/highlights/srt-jobs", status_code=202)
async def srt_job(file: UploadFile = File(...), max_clip_duration_seconds: int = Form(60),
                  min_clip_duration_seconds: int = Form(20)):
    srt = (await file.read()).decode()
    times = [float(h) * 3600 + float(m) * 60 + float(s.replace(",", "."))
             for h, m, s in re.findall(r"(\d+):(\d+):(\d+,\d+) -->", srt)]
    end = times[-1] if times else 200
    length = max(min_clip_duration_seconds, min(42, max_clip_duration_seconds))
    starts = [end * f for f in (0.05, 0.25, 0.45, 0.65)] if end > length * 4 else [0]
    titles = ["Nobody Tells You This", "Three Years Behind", "What Changed My Mind", "Look Stupid in Public"]
    clips = [{
        "clip_id": f"clip_{n:03d}", "rank": n + 1, "start_seconds": round(s, 2),
        "end_seconds": round(min(end, s + length - n * 4), 2), "title": titles[n],
        "best_headline": titles[n], "best_description": "A placeholder clip from the fake Highlightr.",
        "scores": {"overall": round(0.92 - n * 0.07, 2)}, "category": {"primary": "insight"},
    } for n, s in enumerate(starts)]
    JOBS["j"] = {"clips": clips}
    return {"job_id": "j", "status": "queued", "stage": "queued",
            "progress": {"windows_done": 0, "windows_total": 3}}


POLLS = {"n": 0}


@highlightr.get("/v2/highlights/jobs/{job_id}")
def job(job_id: str):
    POLLS["n"] += 1
    done = min(3, POLLS["n"])
    return {"job_id": job_id, "status": "completed" if done == 3 else "running", "stage": "discovery",
            "progress": {"windows_done": done, "windows_total": 3}}


@highlightr.get("/v2/highlights/jobs/{job_id}/result")
def result(job_id: str):
    POLLS["n"] = 0
    return JOBS[job_id]


def _vector(text: str, dimensions: int) -> list[float]:
    out = [0.0] * dimensions
    for word in re.findall(r"\w+", text.split("\nQuery: ", 1)[-1].lower()):
        out[int(hashlib.sha256(word.encode()).hexdigest(), 16) % dimensions] += 1.0
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]


@embeddings.post("/v1/embeddings")
def embed(body: dict = Body(...)):
    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
    dimensions = int(body.get("dimensions") or 1024)
    return {"model": body.get("model"), "object": "list",
            "data": [{"index": i, "embedding": _vector(t, dimensions)} for i, t in enumerate(texts)]}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--parakeet-port", type=int, default=5092)
    parser.add_argument("--highlightr-port", type=int, default=8000)
    parser.add_argument("--embeddings-port", type=int, default=8098)
    args = parser.parse_args()
    for app, port in ((parakeet, args.parakeet_port), (embeddings, args.embeddings_port)):
        threading.Thread(
            target=uvicorn.run,
            args=(app,),
            kwargs={"host": "127.0.0.1", "port": port, "log_level": "warning"},
            daemon=True,
        ).start()
    uvicorn.run(highlightr, host="127.0.0.1", port=args.highlightr_port, log_level="warning")
