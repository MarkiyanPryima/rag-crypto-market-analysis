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

# Pre-download the embedding model into the runtime user's cache. Done after
# USER so the cache lands in /home/worker, where the process can read it —
# downloading as root would leave it unreadable and silently re-fetch on first
# use. Also means the container needs no network access to embed.
RUN python -c "\
from fastembed import TextEmbedding; \
TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2')"

CMD ["python", "-m", "worker.worker"]
