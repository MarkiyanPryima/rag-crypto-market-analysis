.PHONY: install lint format test test-unit db-migrate db-rollback db-migrations db-new

install:
	pip install -r requirements-dev.txt

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

test:
	pytest

# Skip anything needing a live Postgres or Redis.
test-unit:
	pytest -m "not integration"

# Integration tests against a DISPOSABLE database.
#
# These tests DELETE table contents to start from a known state. Pointing them
# at the working database destroys collected data — that is not hypothetical,
# it cost 2520 price snapshots on 13 Oct. conftest.py now skips them unless
# POSTGRES_DB contains "test", and this target supplies such a database,
# recreating it each run so results never depend on leftovers.
TEST_DB = crypto_rag_test

test-integration:
	@set -a && . ./.env && set +a && \
	docker exec $(PG_CONTAINER) psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -q \
		-c "DROP DATABASE IF EXISTS $(TEST_DB)" \
		-c "CREATE DATABASE $(TEST_DB)" && \
	docker exec -i $(PG_CONTAINER) psql -U "$$POSTGRES_USER" -d $(TEST_DB) -q < bdd/schema.sql && \
	docker exec -i $(PG_CONTAINER) psql -U "$$POSTGRES_USER" -d $(TEST_DB) -q < bdd/seed.sql && \
	POSTGRES_HOST=localhost POSTGRES_PORT="$$POSTGRES_HOST_PORT" POSTGRES_DB=$(TEST_DB) \
		pytest -q; \
	status=$$?; \
	docker exec $(PG_CONTAINER) psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -q \
		-c "DROP DATABASE IF EXISTS $(TEST_DB)"; \
	exit $$status

PG_CONTAINER = crypto_rag_postgres

# --- Schema migrations ------------------------------------------------------
#
# There is deliberately no yoyo.ini: the connection URL contains the password,
# so it is built from .env at run time and never committed. --no-config-file
# stops yoyo picking up any stray config.
#
# bdd/schema.sql builds a fresh database; these migrations carry it forward
# without destroying data. Once collectors are running, change the schema with
# `make db-new`, never by editing schema.sql.

YOYO = set -a && . ./.env && set +a && yoyo
YOYO_DB = --no-config-file --database \
	"postgresql+psycopg://$$POSTGRES_USER:$$POSTGRES_PASSWORD@localhost:$$POSTGRES_HOST_PORT/$$POSTGRES_DB"

db-migrate:
	@$(YOYO) apply --batch $(YOYO_DB) bdd/migrations

db-rollback:
	@$(YOYO) rollback --batch $(YOYO_DB) bdd/migrations

db-migrations:
	@$(YOYO) list $(YOYO_DB) bdd/migrations

# usage: make db-new NAME="add volume column to price_snapshots"
db-new:
	@test -n "$(NAME)" || { echo 'usage: make db-new NAME="short description"'; exit 1; }
	@$(YOYO) new --sql --batch $(YOYO_DB) -m "$(NAME)" bdd/migrations
