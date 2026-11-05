DC := docker compose
RUN := $(DC) run --rm dev

.PHONY: up down build sync lint fmt type test eval migrate migrate-check ci demo demo-down web-install web-ci web-up web-smoke web-reresolve-check web-merchant-key docs-diagram

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

# R2-3 / P0-15 gate: golden SQ1-SQ4 E2E, custom metrics, cross-tenant leakage and judge goldens, then the
# report table. Any failed gate (one leak is enough) fails the target. LLM-judged metrics are reported skipped.
EVAL_REPORT := /tmp/eval-report.json
eval:
	$(RUN) sh -c 'set -e; EVAL_REPORT_PATH=$(EVAL_REPORT) uv run --frozen pytest -q \
	  services/ops-ai/src/ops_ai/evaluation services/ops-ai/src/ops_ai/judge; \
	  uv run --frozen python -m ops_ai.evaluation show $(EVAL_REPORT)'

ci: up sync lint type migrate-check test eval web-ci

# runs every service from the app image, then the end-to-end walkthrough (no .env, no OPENAI_API_KEY needed)
demo:
	$(DC) build demo-secrets
	$(DC) up -d --wait gateway-api relay ops-ai-api ops-ai-consumer
	$(DC) --profile demo run --rm demo

demo-down:
	$(DC) --profile demo down

# web client (docs/specs/web.md): node only inside the pinned image, lockfile installs only
WEB_RUN := $(DC) --profile web-dev run --rm --no-deps web-dev

web-install:
	$(WEB_RUN) npm ci --ignore-scripts

web-ci: web-install
	$(WEB_RUN) npm run ci

web-up:
	$(DC) up -d --wait gateway-api relay ops-ai-api ops-ai-consumer
	$(DC) up -d --build web

# prints a fresh demo merchant API key once (stdout only) for the web merchant sign-in; run `make demo` first for payments
web-merchant-key:
	@$(DC) build -q demo-secrets
	@$(DC) up -d --wait gateway-api >/dev/null
	@$(DC) --profile demo run --rm --no-deps demo python scripts/web_merchant_key.py

# full-stack browser smoke; not part of `make ci`
web-smoke: web-up web-install
	@key=$$($(MAKE) -s web-merchant-key 2>/dev/null) && WEB_DEMO_MERCHANT_KEY="$$key" \
	  $(DC) --profile web-smoke run --rm -e WEB_DEMO_MERCHANT_KEY web-smoke npx playwright test

# regression: web (nginx) must keep proxying after gateway-api / ops-ai-api are recreated, without a web restart
web-reresolve-check:
	$(MAKE) web-up
	DC='$(DC)' sh scripts/web_reresolve_check.sh

# DL1: renders the first Mermaid block of docs/architecture.md (runtime topology); mmdc only in the pinned image
MMDC_IMAGE := minlag/mermaid-cli:11.4.2
MMDC := docker run --rm -i -u $$(id -u):$$(id -g) -e HOME=/tmp -v $(CURDIR)/docs:/data $(MMDC_IMAGE)
ARCH_MMD := awk '/^```mermaid/ {n++; if (n == 1) {f = 1; next}} f && /^```/ {exit} f' docs/architecture.md

docs-diagram:
	$(ARCH_MMD) | $(MMDC) -i - -o /data/architecture.png -b white -s 2
	$(ARCH_MMD) | $(MMDC) -i - -o /data/architecture.pdf -b white --pdfFit
