# Shared image for all three collectors. The compose services differ only by
# `command:`, which is why adding the sentiment collector later costs one
# compose block rather than a new build context.
#
# The build context is the repository root, not ./collectors, so the code is
# copied as a `collectors/` package and `python -m collectors.price_collector`
# resolves imports identically in Docker and under pytest. A flat copy would
# need `from config import ...` in Docker but `from collectors.config import ...`
# in tests.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copied before the source so the dependency layer is cached and only
# reinstalls when requirements.txt itself changes.
COPY collectors/requirements.txt collectors/requirements.txt
RUN pip install --no-cache-dir -r collectors/requirements.txt

COPY collectors/ collectors/

# Nothing here needs root. Read-only access to external APIs and append-only
# access to Redis is the whole job, so the container drops privileges.
RUN useradd --create-home --uid 10001 collector
USER collector

# No CMD yet: the collector entry points arrive in later steps and each compose
# service sets `command:` explicitly. A default pointing at a module that does
# not exist would fail confusingly.
