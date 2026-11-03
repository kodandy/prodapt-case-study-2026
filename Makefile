DC := docker compose
RUN := $(DC) run --rm dev

.PHONY: up down build sync lint fmt type test migrate migrate-check ci

up:
	$(DC) up -d --wait postgres redis

down:
	$(DC) down

build:
	$(DC) --profile dev build dev

sync: build
	$(RUN) uv sync --frozen

lint:
	$(RUN) uv run --frozen ruff check .
	$(RUN) uv run --frozen ruff format --check .

fmt:
	$(RUN) uv run --frozen ruff format .
	$(RUN) uv run --frozen ruff check --fix .

type:
	$(RUN) uv run --frozen mypy contracts/src services/gateway/src services/ops-ai/src scripts/generate_dataset.py

migrate:
	$(RUN) uv run --frozen alembic -c services/gateway/alembic.ini upgrade head
	$(RUN) uv run --frozen alembic -c services/ops-ai/alembic.ini upgrade head

# round-trip each history (upgrade, downgrade one step, upgrade) and fail on model/migration drift
migrate-check:
	$(RUN) sh -c 'set -e; for svc in gateway ops-ai; do \
	  cfg=services/$$svc/alembic.ini; \
	  uv run --frozen alembic -c $$cfg upgrade head; \
	  uv run --frozen alembic -c $$cfg downgrade -1; \
	  uv run --frozen alembic -c $$cfg upgrade head; \
	  uv run --frozen alembic -c $$cfg check; \
	done'

test:
	$(RUN) uv run --frozen pytest

ci: up sync lint type migrate-check test
