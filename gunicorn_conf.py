"""Gunicorn configuration for production deployment."""

import os

# Bind
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Workers — uvicorn workers for async support. The app is I/O-bound (FACEIT and Postgres), and
# each worker has its own FACEIT client and concurrency cap, so a few workers go a long way.
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY", 2))

# Timeout — SSE connections can be long-lived
timeout = 300
keepalive = 5

# Logging
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")

# Graceful restart
graceful_timeout = 30
