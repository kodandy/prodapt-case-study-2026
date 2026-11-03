FROM python:3.12.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

# dependencies first, so source edits do not re-resolve them
COPY pyproject.toml uv.lock ./
COPY contracts/pyproject.toml contracts/
COPY services/gateway/pyproject.toml services/gateway/
COPY services/ops-ai/pyproject.toml services/ops-ai/
RUN uv sync --locked --no-dev --no-install-workspace

COPY contracts contracts
COPY services services
COPY scripts scripts
RUN uv sync --locked --no-dev --no-editable \
    && useradd --uid 10001 --no-create-home app \
    && mkdir -p /run/demo-secrets/gateway /run/demo-secrets/ops-ai \
    && chown -R app:app /run/demo-secrets
USER app
