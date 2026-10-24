# Targets load .env so DB_* variables reach docker compose and the CLI.
SHELL := /bin/bash
UV ?= uv
ENV = set -a && . ./.env && set +a &&

.PHONY: up down logs migrate downgrade test test-unit lint format psql

# Whole system: db, migrate, collector, forecaster, api (localhost:8001), ui (localhost:8090).
up:
	docker compose up -d --build --wait

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

migrate:
	$(UV) run alembic upgrade head

downgrade:
	$(UV) run alembic downgrade base

# Integration tests run only against TEST_DATABASE_URL, which they recreate.
test:
	$(UV) run pytest

test-unit:
	$(UV) run pytest -m "not integration"

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

psql:
	@$(ENV) docker exec -it crypto_rag_v2_db psql -U "$$DB_USER" -d "$$DB_NAME"

# --- Full stack ---------------------------------------------------------------
.PHONY: build ps ui-dev

build:
	docker compose build

ps:
	docker compose ps

ui-dev:
	npm --prefix ui run dev
