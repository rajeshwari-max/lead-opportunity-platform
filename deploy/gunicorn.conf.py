"""Gunicorn config for the Lead Scanning Platform API on EC2.

Mirrors the PH-EBS pattern (Gunicorn under Supervisor, behind Nginx) with one
important difference: this is a FastAPI/ASGI app, so it runs under Uvicorn
workers rather than the WSGI default.
"""

# Loopback only. Nginx is the sole public entry point; the API must never be
# reachable directly from the internet. 8001 avoids the 8000 that a Django
# Gunicorn commonly holds — check with `sudo ss -ltnp | grep 800` before use.
bind = "127.0.0.1:8001"

# FastAPI is ASGI. The WSGI default worker would fail to start the app at all.
worker_class = "uvicorn.workers.UvicornWorker"

# ---------------------------------------------------------------------------
# ONE WORKER. This is not a performance oversight — it is required.
#
# The app starts an APScheduler instance in its FastAPI lifespan, and runs the
# scraper as in-process threads. Each Gunicorn worker is a separate process
# that would run its own copy of both, which means N workers produce:
#   * N scheduled scrapes firing simultaneously against the same sources
#   * N digest emails to every team member on every schedule tick
#   * N processes writing to one SQLite file, contending on the write lock
#
# The dashboard is used by a handful of people, so a single worker is ample.
# If concurrency ever becomes the bottleneck, the fix is to split the scheduler
# and scraper into their own service — not to raise this number.
# ---------------------------------------------------------------------------
workers = 1

# Uvicorn workers treat this as a heartbeat. A broad scrape across ~80 sources
# is thread-bound and has been observed to starve the event loop briefly; the
# stock 30s would have Gunicorn kill a worker mid-scrape.
timeout = 300
graceful_timeout = 60
keepalive = 5

# Log paths are derived from this file's own location rather than hard-coded.
# An absolute path here means the checkout directory must be named exactly
# "lead-scanning" or Gunicorn cannot open its log file and Supervisor reports a
# bare "spawn error" — which says nothing about the real cause. Deriving them
# lets the repo sit under any directory name.
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[1]      # the repo root
_LOGS = _ROOT / "logs"
_LOGS.mkdir(parents=True, exist_ok=True)          # Gunicorn will not create it

accesslog = str(_LOGS / "gunicorn-access.log")
errorlog = str(_LOGS / "gunicorn-error.log")
loglevel = "info"

# Nginx sets X-Forwarded-*; trust it so request logs show the real client IP.
forwarded_allow_ips = "127.0.0.1"
