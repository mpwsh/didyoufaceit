"""Gunicorn configuration for production deployment."""

import multiprocessing
import os

# Bind
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Workers — uvicorn workers for async support
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))

# Timeout — SSE connections can be long-lived
timeout = 300
keepalive = 5

# Logging
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")

# Graceful restart
graceful_timeout = 30
