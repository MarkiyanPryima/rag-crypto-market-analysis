# Worker image — the only component that writes to Postgres.
#
# Build context is the repository root, not ./worker, so the code is copied as a
# `worker/` package and `python -m worker.worker` resolves imports identically
# in Docker and under pytest. Same reasoning as collectors/Dockerfile.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copied before the source so the dependency layer is cached and only
# reinstalls when requirements.txt itself changes.
COPY worker/requirements.txt worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

COPY worker/ worker/

# Nothing here needs root. The worker talks to Redis and Postgres over the
# network and writes nothing to disk.
RUN useradd --create-home --uid 10002 worker
USER worker

# No CMD yet: the consumer loop arrives in the next step, and the compose
# service will set `command:` explicitly. A default pointing at a module that
# does not exist would fail confusingly.
