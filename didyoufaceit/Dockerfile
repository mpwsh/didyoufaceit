# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

RUN apt-get -y update && \
    apt-get -y install --no-install-recommends build-essential && \
    apt-get -y clean && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /usr/local/bin/

FROM base
WORKDIR /app

ENV UV_LOCKED=true
ENV UV_NO_CACHE=true
ENV UV_COMPILE_BYTECODE=true
ENV UV_NO_SYNC=true
ENV UV_PYTHON_PREFERENCE=only-system

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-editable --no-install-project

# Copy source
COPY src src
COPY gunicorn_conf.py ./
RUN uv sync --no-dev --no-editable --inexact

# Remove build deps
RUN apt-get -y remove --purge build-essential && \
    apt-get -y autoremove && \
    apt-get -y clean && rm -rf /var/lib/apt/lists/*

EXPOSE 5000

# Production: gunicorn with uvicorn workers
CMD ["uv", "run", "gunicorn", "faceit_finder.server:app", "--config", "gunicorn_conf.py"]
