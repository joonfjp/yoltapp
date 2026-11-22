FROM denoland/deno:2.9.7 AS deno
FROM ghcr.io/astral-sh/uv:0.12.1 AS uv

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /usr/bin/deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv uv sync --locked --no-dev
COPY yolt ./yolt
COPY spec/yolt.openapi.json ./spec/yolt.openapi.json

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    DENO_DIR=/tmp/deno
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
EXPOSE 8485
CMD ["python", "-m", "yolt.container"]
