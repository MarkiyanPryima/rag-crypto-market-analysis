.PHONY: install lint format test test-unit

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
