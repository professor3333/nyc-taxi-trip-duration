.PHONY: setup lint format test test-all ingest ingest-zones

setup:        ## Install the locked environment, including dev tools
	uv sync --frozen

lint:         ## Static checks: ruff lint, ruff format check, mypy on src/
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:       ## Rewrite files to ruff's format (only target that mutates code)
	uv run ruff format .

test:         ## Fast tests only (what CI runs)
	uv run pytest -m "not slow"

test-all:     ## Every test including slow ones
	uv run pytest

ingest:       ## Download + normalise one month: make ingest MONTH=2024-07
	uv run python scripts/ingest.py --service yellow --month $(MONTH)

ingest-zones: ## Fetch the TLC zone lookup CSV
	uv run python scripts/ingest.py --zones
