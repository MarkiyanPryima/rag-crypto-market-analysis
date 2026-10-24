# One image for every Python service; compose picks the command.
FROM python:3.12.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# LightGBM needs the OpenMP runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code edits do not invalidate this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY crypto_rag ./crypto_rag
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 app && chown -R app /app
USER app

CMD ["uvicorn", "crypto_rag.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
