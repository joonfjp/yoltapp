.DEFAULT_GOAL := run
.PHONY: run stop dev setup test fake db db-stop db-rotate-password services services-pull services-stop embed speakers .env

# finch's docker context has no compose plugin; use finch's own.
COMPOSE ?= $(shell [ "$$(docker context show 2>/dev/null)" = finch ] && echo finch compose || echo docker compose)
WITH_ENV = set -a && . ./.env && set +a &&

# Start the full stack, with Yolt at http://127.0.0.1:8485 (YOLT_PORT in .env)
run: .env
	$(WITH_ENV) mkdir -p "$${YOLT_DATA_DIR:-./data}" && \
	  YOLT_UID=$$(id -u) YOLT_GID=$$(id -g) $(COMPOSE) up -d --build

stop:
	$(COMPOSE) stop yolt

# Run on the host, with Python and browser live reload.
dev: db
	$(WITH_ENV) uv run python -m yolt --dev

# PostgreSQL in a container on 127.0.0.1:5489 (YOLT_DB_PORT). Data persists in the
# project's yolt-db volume.
db: .env
	$(COMPOSE) up -d db
	@for i in $$(seq 60); do \
	  $(COMPOSE) exec -T db pg_isready -q -U yolt -d yolt >/dev/null 2>&1 && exit 0; sleep 1; \
	done; echo "PostgreSQL didn't become ready; see: $(COMPOSE) logs db" >&2; exit 1

db-stop:
	$(COMPOSE) stop db

# Published RipeClips images; no source checkout or local build needed.
services:
	$(COMPOSE) up -d --pull always parakeet-cpu highlightr-api

services-pull:
	$(COMPOSE) pull parakeet-cpu highlightr-api

services-stop:
	$(COMPOSE) stop parakeet-cpu highlightr-api

# Initialize missing database settings, including when .env was copied from the example.
.env:
	@sh dev/init_env.sh

# New random database password, applied to the running database, .env and .db-password.
db-rotate-password:
	uv run python dev/rotate_db_password.py

# Embed every clip and passage still missing a vector (the worker also catches up when idle).
embed: db
	$(WITH_ENV) uv run python -m yolt.embed

# Infer speakers for every episode still missing them (the worker also catches up when idle).
speakers: db
	$(WITH_ENV) uv run python -m yolt.speakers

setup:
	uv sync

# Tests start their own throwaway PostgreSQL (Unix socket only); no `make db` needed.
test:
	uv run pytest -q

# Placeholder Parakeet (:5092), Highlightr (:8000) and embeddings (:8098) for UI work without a GPU/LLM.
fake:
	uv run python dev/fake_services.py
