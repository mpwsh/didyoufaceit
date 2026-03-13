# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.9-python3.12-trixie /uv /usr/local/bin/

WORKDIR /app

ENV UV_LOCKED=true \
  UV_NO_CACHE=true \
  UV_COMPILE_BYTECODE=true \
  UV_PYTHON_PREFERENCE=only-system

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-editable --no-install-project

COPY src src
COPY gunicorn_conf.py ./
RUN uv sync --no-dev --no-editable --inexact

FROM python:3.13-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.10.9-python3.12-trixie /uv /usr/local/bin/
COPY --from=builder /app /app

WORKDIR /app

ENV UV_NO_CACHE=true \
  UV_PYTHON_PREFERENCE=only-system

EXPOSE 5000
CMD ["uv", "run", "gunicorn", "didyoufaceit.server:app", "--config", "gunicorn_conf.py"]
